"""Batch 1 unit tests for Team Model Catalog (d051).

Covers all 5 Batch 1 acceptance scenario groups from
``docs/dalton/features/team-model-catalog.md`` §8:
1. Revision confirmation, worker cache, CAS 409, and 3-auth-mode admin guard (local/supabase/oss).
2. Turn effective_catalog_snapshot isolation (pricing/notional/modalities), cross-deploy
   recovery with key_fingerprint revocation, and conditional legacy run backfill.
3. Resolved-address personal custom_models blocking, zero env_key/holder-key inheritance
   across outbound paths, and Fake-IP DoH vs HeldTransport CatalogVariantTransport.
4. Cash & notional pricing ``(provider, norm_id(model_id))`` unique key, 400 ambiguity
   rejection, deep-merge null-delete, narrow pinned route, and upgrade same-name promotion.
5. Outermost retire lock ordering, chained ``A -> B -> C`` rollback, path-only rollback
   ``409 variant_credential_required``, and legacy native_sync_overlay import / degraded
   protection / readonly retired restore lock.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, create_autospec

import httpx
import pytest
from fastapi import HTTPException

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.credentials import (
    enforce_shared_byok_endpoint_policy,
    is_catalog_admin,
    is_catalog_admin_async,
    require_catalog_admin,
)
from src.dalton.llm.notional_pricing import (
    build_notional_price_index_for_catalog,
    check_notional_pricing_ambiguity,
    notional_pair_key,
    resolve_notional_card,
)
from src.dalton.services.team_model_catalog.compiled import (
    compile_catalog_from_payload,
    get_compiled_catalog_async,
    invalidate_compiled_catalog_cache,
    reset_catalog_caches_for_tests,
)
from src.dalton.services.team_model_catalog.constants import (
    CATALOG_SCHEMA,
    TURN_SNAPSHOT_SCHEMA,
    CatalogConflictError,
    CatalogValidationError,
    CatalogVariantCredentialRevokedError,
    CatalogVariantSecurityError,
    catalog_variant_source_tag,
    make_key_fingerprint,
)
from src.dalton.services.team_model_catalog.legacy_import import (
    extract_disk_legacy_retired_readonly,
    run_startup_legacy_sync_import,
)
from src.dalton.services.team_model_catalog.service import (
    assert_model_not_retired_under_lock,
    resolve_chained_side_effect_target,
    restore_catalog_model,
    rollback_catalog,
)
from src.dalton.services.team_model_catalog.snapshot import (
    bind_catalog_snapshot,
    build_effective_catalog_snapshot,
    ensure_run_catalog_snapshot,
)
from src.dalton.services.team_model_catalog.transport import (
    CatalogVariantHttpxTransport,
    enforce_catalog_variant_construction,
    make_catalog_variant_transport_factory,
    resolve_catalog_variant_binding_sync,
)
from src.dalton.services.team_model_catalog.validation import (
    CatalogRollbackInput,
    ModelRestoreInput,
)
from src.llms.llm import LLM
from src.llms.pricing_utils import find_model_pricing
from src.ptc_agent.agent.middleware.file_operations.multimodal_strip import (
    MultimodalStripMiddleware,
)
from src.server.services.llm.byok import byok_candidates, key_fits
from src.server.services.llm.custom_catalog import refuse_custom_model_endpoints
from src.server.services.llm.egress import is_operator_endpoint
from src.server.utils.egress_guard import EgressBlockedError
from src.utils.tracking.core import PricingResolver


@pytest.fixture(autouse=True)
def _isolate_team_catalog_process_state():
    """Prevent compiled catalog, ModelConfig, provider_catalog, or contextvar state from leaking across tests."""
    reset_catalog_caches_for_tests()
    try:
        yield
    finally:
        reset_catalog_caches_for_tests()


# ---------------------------------------------------------------------------
# Minimal deterministic manifest fixtures
# ---------------------------------------------------------------------------


def _base_upstream_models() -> dict[str, Any]:
    return {
        "gpt-test": {
            "model_id": "gpt-test",
            "provider": "openai",
            "display_name": "GPT Test",
            "visible": True,
            "context": 128000,
            "input_modalities": ["text", "image"],
            "parameters": {"max_tokens": 8192},
        },
        "claude-test-oauth": {
            "model_id": "claude-test",
            "provider": "claude-oauth",
            "display_name": "Claude Test OAuth",
            "visible": True,
            "context": 200000,
            "input_modalities": ["text", "image", "pdf"],
        },
        "model-a": {
            "model_id": "model-a",
            "provider": "openai",
            "display_name": "Model A",
            "visible": True,
            "context": 128000,
            "input_modalities": ["text"],
        },
        "model-b": {
            "model_id": "model-b",
            "provider": "openai",
            "display_name": "Model B",
            "visible": True,
            "context": 128000,
            "input_modalities": ["text"],
        },
        "model-c": {
            "model_id": "model-c",
            "provider": "openai",
            "display_name": "Model C",
            "visible": True,
            "context": 128000,
            "input_modalities": ["text"],
        },
    }


def _base_upstream_providers() -> dict[str, Any]:
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
            "claude-oauth": {
                "display_name": "Claude OAuth",
                "sdk": "anthropic",
                "access_type": "oauth",
                "billing": "subscription",
            },
            "z-ai": {
                "display_name": "Z.AI",
                "sdk": "openai",
                "env_key": "ZAI_API_KEY",
                "base_url": "https://api.z.ai/v1",
                "access_type": "api_key",
                "billing": "cash",
                "variants": {
                    "z-ai-cn-coding": {
                        "display_name": "GLM Coding Plan",
                        "access_type": "coding_plan",
                        "billing": "subscription",
                        "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
                    }
                },
            },
        },
        "models": {
            "openai": [
                {
                    "id": "gpt-test",
                    "name": "GPT Test",
                    "pricing": {"input": 2.0, "cached_input": 0.5, "output": 8.0},
                },
                {
                    "id": "model-a",
                    "name": "Model A",
                    "pricing": {"input": 1.0, "output": 2.0},
                },
                {
                    "id": "model-b",
                    "name": "Model B",
                    "pricing": {"input": 1.0, "output": 2.0},
                },
                {
                    "id": "model-c",
                    "name": "Model C",
                    "pricing": {"input": 1.0, "output": 2.0},
                },
            ]
        },
    }


def _base_native_overlay() -> dict[str, Any]:
    return {
        "schema": "dalton-native-overlay/1",
        "models": {
            "grok-4.7-oauth": {
                "model_id": "grok-4.7",
                "provider": "xai-oauth",
                "display_name": "Grok 4.7",
                "visible": True,
                "intelligence": 4,
                "speed": 4,
                "plain_language": 4,
                "context": 256000,
            }
        },
        "providers": {
            "provider_config": {
                "xai-oauth": {
                    "display_name": "xAI SuperGrok",
                    "sdk": "openai",
                    "access_type": "oauth",
                    "billing": "subscription",
                }
            },
            "models": {},
        },
        "dalton_meta": {
            "grok-4.7-oauth": {
                "family": "xai-grok",
                "billing": "subscription",
                "notional_ref": "gpt-test",
                "tool_calling": True,
            }
        },
        "retired": [{"key": "factory-dead-model", "provider": "openai"}],
    }


def _compile_test_catalog(
    payload: dict[str, Any] | None = None,
    *,
    revision: int = 1,
    oauth_sync_enabled: bool = True,
    legacy_sync_import_status: str = "not_applicable",
    last_known_legacy_retired: list[str] | None = None,
    extra_readonly_legacy_retired: set[str] | None = None,
    native_overlay: dict[str, Any] | None = None,
):
    return compile_catalog_from_payload(
        payload or {"schema": CATALOG_SCHEMA},
        revision=revision,
        oauth_sync_enabled=oauth_sync_enabled,
        legacy_sync_import_status=legacy_sync_import_status,
        last_known_legacy_retired=last_known_legacy_retired,
        extra_readonly_legacy_retired=extra_readonly_legacy_retired,
        native_overlay_override=native_overlay if native_overlay is not None else _base_native_overlay(),
        upstream_models_override=_base_upstream_models(),
        upstream_providers_override=_base_upstream_providers(),
    )


# ---------------------------------------------------------------------------
# Group 1: Three-auth-mode admin guard, revision cache & capability gating
# ---------------------------------------------------------------------------


class TestAuthModesAndRevisionCache:
    def test_oss_mode_treats_non_company_owner_as_admin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("src.dalton.settings.get_dalton_auth_mode", lambda: "oss")
        monkeypatch.setattr("src.dalton.credentials.is_dalton_local_auth_enabled", lambda: False)
        monkeypatch.setattr("src.dalton.credentials.is_dalton_supabase_auth_enabled", lambda: False)

        assert is_catalog_admin("local-dev-user") is True
        require_catalog_admin("local-dev-user")
        assert is_catalog_admin(COMPANY_SYSTEM_USER_ID) is False
        with pytest.raises(HTTPException) as exc_info:
            require_catalog_admin(COMPANY_SYSTEM_USER_ID)
        assert exc_info.value.status_code == 403

    def test_supabase_mode_requires_pm_user_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("src.dalton.settings.get_dalton_auth_mode", lambda: "supabase")
        monkeypatch.setattr("src.dalton.credentials.is_dalton_local_auth_enabled", lambda: False)
        monkeypatch.setattr("src.dalton.credentials.is_dalton_supabase_auth_enabled", lambda: True)
        monkeypatch.setattr("src.dalton.credentials.get_configured_pm_user_ids", lambda: ["pm-1"])

        assert is_catalog_admin("pm-1") is True
        assert is_catalog_admin("member-2") is False
        with pytest.raises(HTTPException) as exc_info:
            require_catalog_admin("member-2")
        assert exc_info.value.status_code == 403

    def test_local_mode_checks_pg_roles_and_fails_closed_503(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("src.dalton.settings.get_dalton_auth_mode", lambda: "local")
        monkeypatch.setattr("src.dalton.credentials.is_dalton_local_auth_enabled", lambda: True)

        async def _fake_role(uid: str, conn: Any = None) -> str:
            if uid == "db-err":
                raise RuntimeError("pg down")
            return "admin" if uid == "local-admin" else "internal"

        monkeypatch.setattr("src.dalton.auth.roles.get_user_role", _fake_role)

        assert asyncio.run(is_catalog_admin_async("local-admin")) is True
        assert asyncio.run(is_catalog_admin_async("local-member")) is False
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(is_catalog_admin_async("db-err"))
        assert exc_info.value.status_code == 503

    def test_revision_jump_immediately_refreshes_compiled_catalog(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        invalidate_compiled_catalog_cache()
        rev_state = {"rev": 1, "price": 2.0}

        async def _fake_head(*, conn: Any = None) -> dict[str, Any]:
            return {
                "revision": rev_state["rev"],
                "payload_sha256": f"sha-{rev_state['rev']}",
                "legacy_sync_import_status": "not_applicable",
                "last_known_legacy_retired": [],
            }

        async def _fake_row(*, conn: Any = None, for_update: bool = False) -> dict[str, Any]:
            return {
                "revision": rev_state["rev"],
                "payload_sha256": f"sha-{rev_state['rev']}",
                "legacy_sync_import_status": "not_applicable",
                "last_known_legacy_retired": [],
                "payload": {
                    "schema": CATALOG_SCHEMA,
                    "providers": {
                        "model_overrides": {
                            "openai": {"gpt-test": {"pricing": {"input": rev_state["price"], "output": 8.0}}}
                        }
                    },
                },
            }

        monkeypatch.setattr("src.dalton.database.team_model_catalog.get_catalog_head", _fake_head)
        monkeypatch.setattr("src.dalton.database.team_model_catalog.get_catalog_row", _fake_row)
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._MODELS_JSON_PATH",
            Path("/nonexistent"),
        )
        # Patch loader helpers to use our tiny fixture
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr("src.dalton.llm.overlay._is_native_oauth_sync_enabled", lambda: False)

        cat1 = asyncio.run(get_compiled_catalog_async())
        assert cat1.revision == 1
        assert cat1.model_config.get_model_info("openai", "gpt-test")["pricing"]["input"] == 2.0

        # Simulate another worker committing revision 2
        rev_state["rev"] = 2
        rev_state["price"] = 9.5
        cat2 = asyncio.run(get_compiled_catalog_async())
        assert cat2.revision == 2
        assert cat2.model_config.get_model_info("openai", "gpt-test")["pricing"]["input"] == 9.5
        invalidate_compiled_catalog_cache()

    def test_oauth_capability_disabled_strips_all_oauth_entries(self) -> None:
        cat = _compile_test_catalog(oauth_sync_enabled=False)
        assert "claude-test-oauth" not in cat.model_config.llm_config
        assert "grok-4.7-oauth" not in cat.model_config.llm_config
        assert "grok-4.7-oauth" not in cat.dalton_meta
        assert "gpt-test" in cat.model_config.llm_config


# ---------------------------------------------------------------------------
# Group 2: Turn effective_catalog_snapshot, cross-deploy revocation & backfill
# ---------------------------------------------------------------------------


class TestTurnSnapshotAndCrossDeployRecovery:
    def test_mid_turn_price_and_modality_changes_use_bound_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload_v1 = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "openai": {
                        "acme-gw": {
                            "display_name": "Acme Gateway",
                            "sdk": "openai",
                            "access_type": "api_key",
                            "billing": "cash",
                            "base_url": "https://gw.example.com/v1",
                        }
                    }
                },
                "models": {
                    "acme-gw": [
                        {
                            "id": "acme-1",
                            "name": "Acme 1",
                            "pricing": {"input": 3.0, "cached_input": 0.3, "output": 12.0},
                        }
                    ]
                },
            },
            "models": {
                "acme-model": {
                    "model_id": "acme-1",
                    "provider": "acme-gw",
                    "display_name": "Acme Model",
                    "visible": True,
                    "context": 128000,
                    "input_modalities": ["text", "image"],
                }
            },
        }
        cat_v1 = _compile_test_catalog(payload_v1, revision=5, oauth_sync_enabled=True)
        fp_v1 = make_key_fingerprint("sk-acme-secret-key-v1")
        snap = build_effective_catalog_snapshot(
            ["acme-model"],
            compiled=cat_v1,
            variant_credentials={
                "acme-gw": {
                    "bound_base_url": "https://gw.example.com/v1",
                    "canonical_bound_url": "https://gw.example.com:443/v1",
                    "key_fingerprint": fp_v1,
                }
            },
        )
        assert snap["catalog_revision"] == 5
        assert snap["models"]["acme-model"]["bound_base_url"] == "https://gw.example.com/v1"
        assert snap["models"]["acme-model"]["key_fingerprint"] == fp_v1

        # Simulate mid-turn catalog change (new price 99.0 + text-only modality)
        payload_v2 = {
            **payload_v1,
            "models": {
                "acme-model": {
                    **payload_v1["models"]["acme-model"],
                    "input_modalities": ["text"],
                }
            },
            "providers": {
                **payload_v1["providers"],
                "models": {
                    "acme-gw": [
                        {
                            "id": "acme-1",
                            "name": "Acme 1",
                            "pricing": {"input": 99.0, "output": 199.0},
                        }
                    ]
                },
            },
        }
        cat_v2 = _compile_test_catalog(payload_v2, revision=6, oauth_sync_enabled=True)
        monkeypatch.setattr(LLM, "get_model_config", classmethod(lambda cls: cat_v2.model_config))

        with bind_catalog_snapshot(snap):
            # Pricing lookup & PricingResolver honor v1 snapshot rates (3.0 / 12.0), NOT 99.0
            card = find_model_pricing("acme-1", provider="acme-gw")
            assert card is not None and card["input"] == 3.0 and card["output"] == 12.0
            resolver = PricingResolver()
            rec_card, listed = resolver.card_for(
                {
                    "model_name": "acme-model",
                    "billing_type": "byok",
                    "pricing_model_id": "acme-1",
                    "pricing_provider": "acme-gw",
                }
            )
            assert listed is True
            assert rec_card is not None and rec_card["input"] == 3.0

            # MultimodalStripMiddleware honors v1 snapshot modalities (["text", "image"])
            mw = MultimodalStripMiddleware(model_name="acme-model")

            class _FakeClient:
                metadata = {"manifest_model": "acme-model"}

            stamped, mods = mw._target(_FakeClient())
            assert stamped == "acme-model"
            assert mods == ["text", "image"]

            # Cross-deploy resume with rotated API key on SAME bound_base_url raises CredentialRevokedError
            monkeypatch.setattr(
                "src.dalton.database.team_model_catalog.get_catalog_provider_credential_sync",
                lambda slug, decrypt=True: {
                    "provider_slug": slug,
                    "api_key": "sk-acme-rotated-key-v2",
                    "bound_base_url": "https://gw.example.com/v1",
                    "canonical_bound_url": "https://gw.example.com:443/v1",
                    "key_fingerprint": make_key_fingerprint("sk-acme-rotated-key-v2"),
                },
            )
            with pytest.raises(CatalogVariantCredentialRevokedError):
                resolve_catalog_variant_binding_sync("acme-gw")

    def test_legacy_run_without_snapshot_backfills_conditionally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cat = _compile_test_catalog(revision=0, oauth_sync_enabled=True)
        backfilled: list[tuple[str, dict[str, Any]]] = []

        async def _fake_compiled(*, conn: Any = None, force_reload: bool = False):
            return cat

        async def _fake_creds(*, conn: Any = None):
            return {}

        async def _fake_backfill(run_id: str, snap: dict[str, Any], *, is_subagent: bool = False, conn: Any = None) -> bool:
            backfilled.append((run_id, snap))
            return True

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled.get_compiled_catalog_async",
            _fake_compiled,
        )
        monkeypatch.setattr(
            "src.dalton.database.team_model_catalog.list_catalog_provider_credentials",
            _fake_creds,
        )
        monkeypatch.setattr(
            "src.dalton.database.team_model_catalog.backfill_run_catalog_snapshot",
            _fake_backfill,
        )

        # Legacy run referencing active model -> backfills snapshot
        snap = asyncio.run(
            ensure_run_catalog_snapshot(
                {
                    "conversation_response_id": "00000000-0000-0000-0000-000000000001",
                    "metadata": {"model": "gpt-test"},
                }
            )
        )
        assert snap is not None
        assert "gpt-test" in snap["models"]
        assert len(backfilled) == 1

        # Legacy run referencing vanished/unknown model -> gracefully returns None
        missing = asyncio.run(
            ensure_run_catalog_snapshot(
                {
                    "conversation_response_id": "00000000-0000-0000-0000-000000000002",
                    "metadata": {"model": "vanished-legacy-model"},
                }
            )
        )
        assert missing is None


# ---------------------------------------------------------------------------
# Group 3: Variant isolation, resolved-address custom_models block, Fake-IP DoH
# ---------------------------------------------------------------------------


class TestCatalogVariantIsolationAndTransport:
    def _variant_catalog(self):
        payload = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "openai": {
                        "corp-gw": {
                            "display_name": "Corp Gateway",
                            "sdk": "openai",
                            "access_type": "api_key",
                            "billing": "cash",
                            "base_url": "https://corp-llm.example.com/v1",
                        }
                    }
                },
                "models": {
                    "corp-gw": [
                        {
                            "id": "corp-pro",
                            "name": "Corp Pro",
                            "pricing": {"input": 1.0, "output": 3.0},
                        }
                    ]
                },
            },
            "models": {
                "corp-pro-model": {
                    "model_id": "corp-pro",
                    "provider": "corp-gw",
                    "display_name": "Corp Pro",
                    "visible": True,
                    "context": 128000,
                }
            },
        }
        return _compile_test_catalog(payload, revision=3, oauth_sync_enabled=False)

    def test_flatten_strips_env_key_and_byok_blocks_catalog_variant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cat = self._variant_catalog()
        mc = cat.model_config
        info = mc.get_provider_info("corp-gw")
        assert info["catalog_defined"] is True
        assert info["own_api_key"] is True
        assert "env_key" not in info
        assert "models_url" not in info
        assert "platform" not in info

        assert byok_candidates("corp-gw", mc) == []
        assert key_fits("corp-gw", "openai", has_base_url=True, mc=mc) is False
        assert key_fits("corp-gw", "corp-gw", has_base_url=True, mc=mc) is False

        monkeypatch.setattr(LLM, "get_model_config", classmethod(lambda cls: mc))
        assert is_operator_endpoint("https://corp-llm.example.com/v1") is False

    def test_personal_custom_model_inheriting_holder_base_url_blocked_save_and_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cat = self._variant_catalog()
        mc = cat.model_config
        monkeypatch.setattr(LLM, "get_model_config", classmethod(lambda cls: mc))

        # Holder saved openai base_url pointing at corp-gw's bound_base_url
        async def _fake_get_keys(uid: str) -> dict[str, Any]:
            return {
                "byok_enabled": True,
                "keys": {"openai": "sk-holder"},
                "base_urls": {"openai": "https://corp-llm.example.com/v1"},
            }

        monkeypatch.setattr("src.server.database.api_keys.get_user_api_keys", _fake_get_keys)
        monkeypatch.setattr("src.dalton.credentials.uses_shared_team_credential", lambda uid: True)
        monkeypatch.setattr("src.server.services.llm.egress.enforced", lambda: False)

        # Save-time check rejects personal custom model on built-in 'openai' inheriting holder's URL
        with pytest.raises(HTTPException) as save_exc:
            asyncio.run(
                refuse_custom_model_endpoints(
                    [{"name": "my-sneaky", "model_id": "corp-pro", "provider": "openai"}],
                    stored=[],
                    user_id="member-1",
                )
            )
        assert save_exc.value.status_code == 400
        assert save_exc.value.detail["code"] == "catalog_variant_custom_model_forbidden"

        # Call-time check in enforce_shared_byok_endpoint_policy also rejects it
        with pytest.raises(HTTPException) as call_exc:
            enforce_shared_byok_endpoint_policy(
                user_id="member-1",
                model_name="my-sneaky",
                byok_config={"api_key": "sk-holder", "base_url": "https://corp-llm.example.com/v1"},
                base_url="https://corp-llm.example.com/v1",
                manifest_base_url="https://api.openai.com/v1",
                custom_config={"name": "my-sneaky", "model_id": "corp-pro", "provider": "openai"},
                holding_provider="openai",
            )
        assert call_exc.value.status_code == 400
        assert call_exc.value.detail["code"] == "catalog_variant_custom_model_forbidden"

    def test_construct_time_invariant_rejects_missing_tag_or_transport(self) -> None:
        cat = self._variant_catalog()
        info = cat.model_config.get_provider_info("corp-gw")
        factory = make_catalog_variant_transport_factory(
            provider_slug="corp-gw",
            bound_base_url="https://corp-llm.example.com/v1",
            verify_db_binding=False,
        )
        # Missing credential source tag -> rejected
        with pytest.raises(CatalogVariantSecurityError):
            enforce_catalog_variant_construction(
                provider="corp-gw",
                provider_info=info,
                base_url="https://corp-llm.example.com/v1",
                egress_transport=factory,
                follow_redirects=False,
                credential_source=None,
                api_key_override="sk-corp",
            )
        # Redirects enabled -> rejected
        with pytest.raises(CatalogVariantSecurityError):
            enforce_catalog_variant_construction(
                provider="corp-gw",
                provider_info=info,
                base_url="https://corp-llm.example.com/v1",
                egress_transport=factory,
                follow_redirects=True,
                credential_source=catalog_variant_source_tag("corp-gw"),
                api_key_override="sk-corp",
            )
        # Valid configuration passes
        enforce_catalog_variant_construction(
            provider="corp-gw",
            provider_info=info,
            base_url="https://corp-llm.example.com/v1",
            egress_transport=factory,
            follow_redirects=False,
            credential_source=catalog_variant_source_tag("corp-gw"),
            api_key_override="sk-corp",
        )

    def test_transport_fake_ip_doh_dials_real_public_ip_with_sni_and_blocks_private(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ipaddress

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.transport.get_fake_ip_networks",
            lambda: (ipaddress.ip_network("198.18.0.0/15"),),
        )
        monkeypatch.setattr(
            "src.dalton.network.get_fake_ip_networks",
            lambda: (ipaddress.ip_network("198.18.0.0/15"),),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.transport._resolve_host_sync",
            lambda host: ["198.18.0.42"],
        )

        captured_requests: list[httpx.Request] = []
        mock_inner = create_autospec(httpx.AsyncBaseTransport, instance=True)

        async def _fake_handle_async(req: httpx.Request) -> httpx.Response:
            captured_requests.append(req)
            return httpx.Response(200, json={"ok": True})

        mock_inner.handle_async_request = AsyncMock(
            spec=httpx.AsyncBaseTransport.handle_async_request,
            side_effect=_fake_handle_async,
        )

        async def _fake_doh_public(host: str, addrs: list[str], is_blocked):
            assert not is_blocked("93.184.216.34")
            return None, ["93.184.216.34"]

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.transport.fake_ip_block_reason",
            _fake_doh_public,
        )

        transport = CatalogVariantHttpxTransport(
            provider_slug="corp-gw",
            bound_base_url="https://corp-llm.example.com/v1",
            credential_source=catalog_variant_source_tag("corp-gw"),
            verify_db_binding=False,
            direct_async_transport=mock_inner,
        )
        req = httpx.Request("POST", "https://corp-llm.example.com/v1/chat/completions")
        resp = asyncio.run(transport.handle_async_request(req))
        assert resp.status_code == 200
        assert len(captured_requests) == 1
        pinned = captured_requests[0]
        assert pinned.url.host == "93.184.216.34"
        assert pinned.extensions.get("sni_hostname") == "corp-llm.example.com"
        assert pinned.headers.get("host") == "corp-llm.example.com"

        # Path escape outside bound_base_url is refused before any dial
        bad_req = httpx.Request("POST", "https://corp-llm.example.com/v2/chat/completions")
        with pytest.raises(CatalogVariantSecurityError):
            asyncio.run(transport.handle_async_request(bad_req))

        # Private real IP in Path B (no fake-IP configured) is blocked
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.transport.get_fake_ip_networks",
            lambda: (),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.transport._resolve_host_sync",
            lambda host: ["10.0.0.5"],
        )
        with pytest.raises(EgressBlockedError):
            asyncio.run(transport.handle_async_request(req))


# ---------------------------------------------------------------------------
# Group 4: Notional pricing unique pair key, ambiguity 400, merge rules
# ---------------------------------------------------------------------------


class TestNotionalPricingAndMergeSemantics:
    def test_notional_pair_key_separates_providers_and_rejects_same_provider_ambiguity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two different providers sharing model_id "shared-1" keep independent notional cards
        payload_ok = {
            "schema": CATALOG_SCHEMA,
            "models": {
                "shared-zai": {
                    "model_id": "shared-1",
                    "provider": "z-ai-cn-coding",
                    "display_name": "Shared ZAI",
                    "visible": True,
                },
                "shared-claude-oauth": {
                    "model_id": "shared-1",
                    "provider": "claude-oauth",
                    "display_name": "Shared Claude",
                    "visible": True,
                },
            },
            "dalton_meta": {
                "shared-zai": {
                    "billing": "subscription",
                    "notional_pricing": {"input": 1.0, "output": 3.0, "cached_input": 0.1},
                },
                "shared-claude-oauth": {
                    "billing": "subscription",
                    "notional_pricing": {"input": 5.0, "output": 25.0, "cached_input": 0.5},
                },
            },
        }
        cat = _compile_test_catalog(payload_ok, revision=2, oauth_sync_enabled=True)
        idx = build_notional_price_index_for_catalog(
            model_config=cat.model_config, dalton_meta=cat.dalton_meta
        )
        assert notional_pair_key("z-ai-cn-coding", "shared-1") in idx
        assert notional_pair_key("claude-oauth", "shared-1") in idx
        c1 = resolve_notional_card(
            "shared-zai",
            pricing_model_id="shared-1",
            pricing_provider="z-ai-cn-coding",
            index=idx,
        )
        c2 = resolve_notional_card(
            "shared-claude-oauth",
            pricing_model_id="shared-1",
            pricing_provider="claude-oauth",
            index=idx,
        )
        assert c1 is not None and c1["input"] == 1.0
        assert c2 is not None and c2["input"] == 5.0

        # Same provider + same norm_id(model_id) with DIFFERENT notional rates -> ambiguous!
        payload_ambig = {
            "schema": CATALOG_SCHEMA,
            "models": {
                "zai-dot": {
                    "model_id": "glm-5.4",
                    "provider": "z-ai-cn-coding",
                    "display_name": "GLM 5.4 Dot",
                    "visible": True,
                },
                "zai-dash": {
                    "model_id": "glm-5-4",
                    "provider": "z-ai-cn-coding",
                    "display_name": "GLM 5-4 Dash",
                    "visible": True,
                },
            },
            "dalton_meta": {
                "zai-dot": {
                    "billing": "subscription",
                    "notional_pricing": {"input": 1.4, "output": 4.4},
                },
                "zai-dash": {
                    "billing": "subscription",
                    "notional_pricing": {"input": 2.8, "output": 8.8},
                },
            },
        }
        cat_ambig = _compile_test_catalog(payload_ambig, revision=3, oauth_sync_enabled=True)
        conflicts = check_notional_pricing_ambiguity(
            model_config=cat_ambig.model_config, dalton_meta=cat_ambig.dalton_meta
        )
        assert len(conflicts) == 1
        assert conflicts[0]["provider"] == "z-ai-cn-coding"
        assert conflicts[0]["normalized_model_id"] == "glm-5-4"

    def test_narrow_pinned_route_and_deep_merge_null_delete_and_upgrade_promotion(self) -> None:
        payload = {
            "schema": CATALOG_SCHEMA,
            # Attempt to hijack grok-4.7-oauth provider while changing display_name & intelligence
            "model_overrides": {
                "grok-4.7-oauth": {
                    "provider": "openai",
                    "model_id": "hijacked-id",
                    "display_name": "Grok 4.7 Custom Title",
                    "intelligence": 5,
                }
            },
            # Upgrade same-name promotion: DB 'models' has 'gpt-test' which now also exists in factory
            "models": {
                "gpt-test": {
                    "display_name": "GPT Test Promoted",
                    "plain_language": 5,
                }
            },
            "providers": {
                "models": {
                    "openai": [
                        {
                            "id": "gpt-test",
                            "pricing": {"input": 4.2},
                        }
                    ]
                }
            },
            "dalton_meta": {
                "grok-4.7-oauth": {
                    "notional_ref": None,
                    "notional_pricing": {"input": 3.5, "output": 14.0},
                }
            },
        }
        cat = _compile_test_catalog(payload, revision=4, oauth_sync_enabled=True)
        grok = cat.model_config.get_model_config("grok-4.7-oauth")
        assert grok["provider"] == "xai-oauth"
        assert grok["model_id"] == "grok-4.7"
        assert grok["display_name"] == "Grok 4.7 Custom Title"
        assert grok["intelligence"] == 5

        # Null deletion cleared notional_ref while preserving family & tool_calling
        grok_meta = cat.dalton_meta["grok-4.7-oauth"]
        assert "notional_ref" not in grok_meta
        assert grok_meta["family"] == "xai-grok"
        assert grok_meta["notional_pricing"]["input"] == 3.5

        # Upgrade same-name promotion preserved both factory context and admin display_name & price
        gpt = cat.model_config.get_model_config("gpt-test")
        assert gpt["context"] == 128000
        assert gpt["display_name"] == "GPT Test Promoted"
        assert gpt["plain_language"] == 5
        gpt_card = cat.model_config.get_model_info("openai", "gpt-test")
        assert gpt_card["pricing"]["input"] == 4.2
        assert gpt_card["pricing"]["output"] == 8.0


# ---------------------------------------------------------------------------
# Group 5: Chained A->B->C rollback, path-only credential 409, legacy import
# ---------------------------------------------------------------------------


class TestChainedRollbackAndLegacyImport:
    def test_chained_side_effect_restores_a_to_b_to_c_atomically_and_protects_user_edits(
        self,
    ) -> None:
        # Pure A -> B (cv 1->2) -> C (cv 2->3) chain, current DB cv == 3
        hop1 = {
            "kind": "automation",
            "id": "auto-1",
            "before": {"llm_model": "model-a", "reasoning_effort": "high"},
            "after": {"llm_model": "model-b", "reasoning_effort": "medium"},
            "before_config_version": 1,
            "after_config_version": 2,
        }
        hop2 = {
            "kind": "automation",
            "id": "auto-1",
            "before": {"llm_model": "model-b", "reasoning_effort": "medium"},
            "after": {"llm_model": "model-c", "reasoning_effort": "low"},
            "before_config_version": 2,
            "after_config_version": 3,
        }
        target, reason = resolve_chained_side_effect_target(
            [hop2, hop1], current_version=3, version_kind="config_version"
        )
        assert reason is None
        assert target == {"llm_model": "model-a", "reasoning_effort": "high"}

        # User edited task after hop2 (cv == 4) -> skipped completely
        skipped_target, skip_reason = resolve_chained_side_effect_target(
            [hop2, hop1], current_version=4, version_kind="config_version"
        )
        assert skipped_target is None
        assert skip_reason == "user_modified_after_last_replacement"

        # User edited task BETWEEN hop1 (after_cv=2) and hop2 (before_cv=3) -> stops at hop2.before
        hop2_after_user = {**hop2, "before_config_version": 3, "after_config_version": 4}
        mid_target, mid_reason = resolve_chained_side_effect_target(
            [hop2_after_user, hop1], current_version=4, version_kind="config_version"
        )
        assert mid_reason is None
        assert mid_target == {"llm_model": "model-b", "reasoning_effort": "medium"}

    def test_rollback_path_only_url_change_requires_matching_variant_credential_409(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Target revision T=1 had corp-gw at https://corp.example.com/v1
        # Current revision C=2 moved corp-gw to https://corp.example.com/v2 (path-only change)
        payload_t1 = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "openai": {
                        "corp-gw": {
                            "display_name": "Corp GW",
                            "sdk": "openai",
                            "access_type": "api_key",
                            "billing": "cash",
                            "base_url": "https://corp.example.com/v1",
                        }
                    }
                }
            },
        }
        payload_c2 = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "openai": {
                        "corp-gw": {
                            "display_name": "Corp GW",
                            "sdk": "openai",
                            "access_type": "api_key",
                            "billing": "cash",
                            "base_url": "https://corp.example.com/v2",
                        }
                    }
                }
            },
        }

        class _FakeConn:
            @asynccontextmanager
            async def transaction(self):
                yield

        @asynccontextmanager
        async def _fake_get_db(_conn=None):
            yield _FakeConn()

        from src.dalton.services.team_model_catalog import service as tmc_service

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.get_db_connection", _fake_get_db
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.acquire_catalog_exclusive_lock",
            create_autospec(tmc_service.acquire_catalog_exclusive_lock),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.get_catalog_row",
            create_autospec(
                tmc_service.get_catalog_row,
                return_value={
                    "revision": 2,
                    "payload": payload_c2,
                    "payload_sha256": "sha2",
                    "legacy_sync_import_status": "not_applicable",
                    "last_known_legacy_retired": [],
                },
            ),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.get_catalog_revision",
            create_autospec(
                tmc_service.get_catalog_revision,
                return_value={"revision": 1, "payload": payload_t1},
            ),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.list_catalog_provider_credentials",
            create_autospec(
                tmc_service.list_catalog_provider_credentials,
                return_value={
                    "corp-gw": {
                        "provider_slug": "corp-gw",
                        "bound_base_url": "https://corp.example.com/v2",
                        "canonical_bound_url": "https://corp.example.com:443/v2",
                        "key_fingerprint": "sk-...2222",
                    }
                },
            ),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr("src.dalton.llm.overlay._is_native_oauth_sync_enabled", lambda: False)

        with pytest.raises(CatalogConflictError) as exc_info:
            asyncio.run(
                rollback_catalog(
                    actor_user_id="admin-1",
                    data=CatalogRollbackInput(target_revision=1, expected_revision=2),
                )
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "variant_credential_required"

    def test_legacy_import_degraded_admission_and_readonly_retired_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 1. Truncated JSON before/inside retired section -> regex extracts what's readable
        truncated = tmp_path / "native_sync_overlay.json"
        truncated.write_text(
            '{"schema":"dalton-native-sync-overlay/1","models":{"synced-new":{"model_id":"s1","provider":"openai"}},"retired":[{"key":"model-b","provider":"openai"',
            encoding="utf-8",
        )
        extracted = extract_disk_legacy_retired_readonly(truncated)
        assert "model-b" in extracted

        # 2. Failed import state: pauses native-sync-added models, allows ONLY factory non-retired models
        cat_failed = _compile_test_catalog(
            {
                "schema": CATALOG_SCHEMA,
                "models": {
                    "synced-new-model": {
                        "model_id": "synced-1",
                        "provider": "openai",
                        "display_name": "Synced New",
                        "visible": True,
                    }
                },
            },
            revision=1,
            oauth_sync_enabled=True,
            legacy_sync_import_status="failed",
            last_known_legacy_retired=["model-a"],
            extra_readonly_legacy_retired={"model-b"},
        )
        assert "synced-new-model" not in cat_failed.model_config.llm_config
        assert "model-a" not in cat_failed.model_config.llm_config
        assert "model-b" not in cat_failed.model_config.llm_config
        assert "gpt-test" in cat_failed.model_config.llm_config
        assert "model-c" in cat_failed.model_config.llm_config

        # 3. Normal restore on readonly legacy retired key returns 409; factory retired returns 400
        class _FakeConn:
            @asynccontextmanager
            async def transaction(self):
                yield

        @asynccontextmanager
        async def _fake_get_db(_conn=None):
            yield _FakeConn()

        from src.dalton.database import team_model_catalog as tmc_db
        from src.dalton.services.team_model_catalog import service as tmc_service

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.get_db_connection", _fake_get_db
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.acquire_catalog_exclusive_lock",
            create_autospec(tmc_service.acquire_catalog_exclusive_lock),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.get_catalog_row",
            create_autospec(
                tmc_service.get_catalog_row,
                return_value={
                    "revision": 1,
                    "payload": {"schema": CATALOG_SCHEMA, "retired": [{"key": "model-a"}]},
                    "payload_sha256": "sha1",
                    "legacy_sync_import_status": "failed",
                    "last_known_legacy_retired": ["model-a"],
                },
            ),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.extract_disk_legacy_retired_readonly",
            lambda legacy_path=None: set(),
        )
        monkeypatch.setattr(
            "src.dalton.database.team_model_catalog.fetch_legacy_retired_history",
            create_autospec(tmc_db.fetch_legacy_retired_history, return_value=set()),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        with pytest.raises(CatalogConflictError) as leg_exc:
            asyncio.run(
                restore_catalog_model(
                    actor_user_id="admin-1",
                    model_key="model-a",
                    data=ModelRestoreInput(expected_revision=1),
                )
            )
        assert leg_exc.value.status_code == 409
        assert leg_exc.value.code == "legacy_retired_locked_until_import_resolved"

        with pytest.raises(CatalogValidationError) as fac_exc:
            asyncio.run(
                restore_catalog_model(
                    actor_user_id="admin-1",
                    model_key="factory-dead-model",
                    data=ModelRestoreInput(expected_revision=1),
                )
            )
        assert fac_exc.value.status_code == 400
        assert fac_exc.value.code == "factory_retired_immutable"

    def test_startup_legacy_import_commits_before_archive_and_handles_missing_file_with_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[str] = []

        class _FakeConn:
            @asynccontextmanager
            async def transaction(self):
                yield
                events.append("db_commit")

        @asynccontextmanager
        async def _fake_get_db(_conn=None):
            yield _FakeConn()

        from src.dalton.services.team_model_catalog import legacy_import as legacy_import_mod

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.get_db_connection", _fake_get_db
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.acquire_catalog_exclusive_lock",
            create_autospec(legacy_import_mod.acquire_catalog_exclusive_lock),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.publish_catalog_changed",
            create_autospec(legacy_import_mod.publish_catalog_changed),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.invalidate_catalog_redis_caches",
            create_autospec(legacy_import_mod.invalidate_catalog_redis_caches),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        # Case A: valid file commits FIRST, then archives file
        valid_file = tmp_path / "native_sync_overlay.json"
        valid_file.write_text(
            json.dumps(
                {
                    "schema": "dalton-native-sync-overlay/1",
                    "models": {
                        "imported-m1": {
                            "model_id": "imported-m1",
                            "provider": "openai",
                            "display_name": "Imported M1",
                            "visible": True,
                        }
                    },
                    "dalton_meta": {"imported-m1": {"tool_calling": True}},
                    "retired": [{"key": "model-b", "provider": "openai"}],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.get_catalog_row",
            create_autospec(
                legacy_import_mod.get_catalog_row,
                return_value={
                    "revision": 0,
                    "payload": {"schema": CATALOG_SCHEMA},
                    "payload_sha256": "sha0",
                    "legacy_sync_import_status": "pending",
                    "legacy_sync_imported_at": None,
                    "last_known_legacy_retired": [],
                },
            ),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.save_catalog_revision",
            create_autospec(
                legacy_import_mod.save_catalog_revision,
                return_value={
                    "catalog": {"revision": 1, "legacy_sync_import_status": "imported"},
                    "revision_record": {"revision": 1},
                },
            ),
        )
        orig_archive = __import__(
            "src.dalton.services.team_model_catalog.legacy_import",
            fromlist=["_archive_legacy_file"],
        )._archive_legacy_file

        def _tracked_archive(path: Path, *, suffix_kind: str = "migrated"):
            events.append(f"archive:{suffix_kind}")
            return orig_archive(path, suffix_kind=suffix_kind)

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import._archive_legacy_file",
            _tracked_archive,
        )

        res = asyncio.run(run_startup_legacy_sync_import(legacy_path=valid_file))
        assert res["status"] == "imported"
        assert events == ["db_commit", "archive:migrated"]
        assert not valid_file.exists()

        # Case B: missing file on deployment WITH native sync history -> enters 'failed', NOT 'not_applicable'
        missing_file = tmp_path / "missing_dir" / "native_sync_overlay.json"
        state_updates: list[str] = []

        async def _fake_update_state(conn, *, status: str, **kwargs):
            state_updates.append(status)
            return {"legacy_sync_import_status": status}

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.has_legacy_native_sync_history",
            create_autospec(legacy_import_mod.has_legacy_native_sync_history, return_value=True),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.fetch_legacy_retired_history",
            create_autospec(
                legacy_import_mod.fetch_legacy_retired_history, return_value={"model-a"}
            ),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.update_legacy_import_state_only",
            _fake_update_state,
        )
        res_b = asyncio.run(run_startup_legacy_sync_import(legacy_path=missing_file))
        assert res_b["status"] == "failed"
        assert state_updates == ["failed"]

    def test_outermost_shared_lock_order_and_in_lock_retired_rejection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        call_order: list[str] = []

        async def _fake_shared(conn: Any) -> None:
            call_order.append("catalog_shared")

        class _FakeConn:
            async def execute(self, sql: str, params: Any = None) -> None:
                if "pg_advisory_xact_lock(" in sql:
                    call_order.append("user_subagents_lock")

        monkeypatch.setattr(
            "src.dalton.database.team_model_catalog.acquire_catalog_shared_lock",
            _fake_shared,
        )
        from src.server.database.user_subagents import lock_user_subagents

        asyncio.run(lock_user_subagents("u-1", conn=_FakeConn()))
        assert call_order == ["catalog_shared", "user_subagents_lock"]

        # In-lock retired check rejects retired model with 400 model_retired
        cat_retired = _compile_test_catalog(
            {"schema": CATALOG_SCHEMA, "retired": [{"key": "model-b", "provider": "openai"}]},
            revision=2,
        )
        from src.dalton.services.team_model_catalog import service as tmc_service

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.get_compiled_catalog_async",
            create_autospec(tmc_service.get_compiled_catalog_async, return_value=cat_retired),
        )
        with pytest.raises(HTTPException) as ret_exc:
            asyncio.run(assert_model_not_retired_under_lock(_FakeConn(), "model-b"))
        assert ret_exc.value.status_code == 400
        assert ret_exc.value.detail["code"] == "model_retired"


# ---------------------------------------------------------------------------
# Group 6: R2 regression tests covering all 7 P1 review findings
# ---------------------------------------------------------------------------


class TestR2Fixes:
    def test_r2_1_personal_and_core_automation_create_update_open_tx_for_catalog_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from uuid import UUID

        from src.dalton.database import team_model_catalog as tmc_db
        from src.dalton.services.team_model_catalog import service as tmc_service
        from src.server.database import automation as auto_db
        from src.server.database import pool as pool_mod
        from src.server.models.automation import AutomationCreate, AutomationUpdate
        from src.server.services.automations import lifecycle as auto_lifecycle

        events: list[str] = []

        class _TxConn:
            @asynccontextmanager
            async def transaction(self):
                events.append("tx_enter")
                yield
                events.append("tx_exit")

            async def execute(self, sql: str, params: Any = None) -> None:
                events.append(f"exec:{sql[:32]}")

        tx_conn = _TxConn()

        @asynccontextmanager
        async def _fake_get_db(conn: Any = None):
            yield conn if conn is not None else tx_conn

        async def _fake_shared_lock(conn: Any) -> None:
            assert conn is not None, "acquire_catalog_shared_lock must never receive conn=None"
            events.append("catalog_shared_lock")

        ws_id = "11111111-1111-4111-8111-111111111111"
        auto_id = "22222222-2222-4222-8222-222222222222"

        monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)
        monkeypatch.setattr(pool_mod, "get_db_connection", _fake_get_db)
        monkeypatch.setattr(
            tmc_db,
            "acquire_catalog_shared_lock",
            create_autospec(tmc_db.acquire_catalog_shared_lock, side_effect=_fake_shared_lock),
        )
        monkeypatch.setattr(
            tmc_service,
            "assert_model_not_retired_under_lock",
            create_autospec(tmc_service.assert_model_not_retired_under_lock, return_value=None),
        )
        monkeypatch.setattr(
            auto_lifecycle,
            "_placement",
            create_autospec(auto_lifecycle._placement, return_value=ws_id),
        )
        monkeypatch.setattr(
            auto_lifecycle,
            "require_target_ownership",
            create_autospec(auto_lifecycle.require_target_ownership, return_value=None),
        )
        monkeypatch.setattr(
            auto_lifecycle,
            "check_claude_code_hosted",
            create_autospec(auto_lifecycle.check_claude_code_hosted, return_value=None),
        )
        monkeypatch.setattr(
            auto_lifecycle,
            "check_model",
            create_autospec(auto_lifecycle.check_model, return_value=("system", {"provider": "openai"})),
        )
        monkeypatch.setattr(
            auto_lifecycle,
            "_reconcile_reasoning_effort",
            create_autospec(auto_lifecycle._reconcile_reasoning_effort, return_value=(False, None)),
        )
        from src.dalton.database import company_automations as comp_auto_db

        monkeypatch.setattr(
            comp_auto_db,
            "resolve_automation_scope_for_write",
            create_autospec(comp_auto_db.resolve_automation_scope_for_write, return_value="personal"),
        )
        monkeypatch.setattr(
            auto_lifecycle.user_models,
            "get_model_preference",
            create_autospec(auto_lifecycle.user_models.get_model_preference, return_value={}),
        )

        create_conns: list[Any] = []

        async def _fake_db_create(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            create_conns.append(kwargs.get("conn"))
            events.append("db_create")
            return {"automation_id": auto_id, "name": kwargs["name"]}

        monkeypatch.setattr(
            auto_lifecycle.auto_db,
            "create_automation",
            create_autospec(auto_db.create_automation, side_effect=_fake_db_create),
        )

        # 1. Personal / standalone-core create with conn=None and default model (llm_model=None)
        asyncio.run(
            auto_lifecycle.create_automation(
                "user-1",
                AutomationCreate(
                    name="Daily Check",
                    trigger_type="cron",
                    cron_expression="0 9 * * *",
                    instruction="Check portfolio",
                    workspace_id=UUID(ws_id),
                ),
                conn=None,
            )
        )
        assert create_conns == [tx_conn]
        assert events == ["tx_enter", "catalog_shared_lock", "db_create", "tx_exit"]

        # 2. Personal / standalone-core update with conn=None switching model
        events.clear()
        update_conns: list[Any] = []

        async def _fake_db_update(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            update_conns.append(kwargs.get("conn"))
            events.append("db_update")
            return {"automation_id": auto_id, "llm_model": kwargs.get("llm_model")}

        monkeypatch.setattr(
            auto_lifecycle.auto_db,
            "update_automation",
            create_autospec(auto_db.update_automation, side_effect=_fake_db_update),
        )
        current_row = {
            "automation_id": auto_id,
            "user_id": "user-1",
            "name": "Daily Check",
            "trigger_type": "cron",
            "cron_expression": "0 9 * * *",
            "timezone": "UTC",
            "workspace_id": ws_id,
            "conversation_thread_id": None,
            "thread_strategy": "new",
            "llm_model": None,
            "reasoning_effort": None,
            "status": "active",
            "scope": "personal",
            "metadata": {},
            "config_version": 1,
        }
        asyncio.run(
            auto_lifecycle.update_automation(
                auto_id,
                "user-1",
                AutomationUpdate(llm_model="gpt-test"),
                conn=None,
                current=current_row,
            )
        )
        assert update_conns == [tx_conn]
        assert events == ["tx_enter", "catalog_shared_lock", "db_update", "tx_exit"]

    def test_r2_2_snapshot_authoritative_server_binding_and_in_flight_settlement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import Request

        from src.dalton.services.team_model_catalog.snapshot import (
            get_active_catalog_snapshot,
            run_with_catalog_snapshot,
        )
        from src.server.app.threads import messaging as messaging_mod
        from src.server.database.runs import subagent_runs as sr_db
        from src.server.models.chat import ChatRequest
        from src.server.services.report_back.restart import OwedResume, resume_turn
        from src.server.services.runs.coordinator import RunCoordinator, RunHandle, RunOutcome
        from src.server.services.subagent_run_coordinator import SubagentRunCoordinator

        # 1. restart.resume_turn sends server-only resume_of_run_id (not untrusted pricing dict)
        owed_msg = OwedResume(
            user_id="u-1",
            workspace_id="ws-1",
            interrupted=True,
            source_run_id="run-old-1",
        )
        route_msg, body_msg = resume_turn(owed_msg, "req-key-1")
        assert route_msg == "messages"
        assert body_msg["resume_of_run_id"] == "run-old-1"
        assert "effective_catalog_snapshot" not in body_msg

        owed_retry = OwedResume(
            user_id="u-1",
            workspace_id="ws-1",
            interrupted=True,
            retry_run_id="run-old-2",
            source_run_id="run-old-2",
        )
        route_retry, body_retry = resume_turn(owed_retry, "req-key-2")
        assert route_retry == "retry"
        assert body_retry["run_id"] == "run-old-2"
        assert "effective_catalog_snapshot" not in body_retry

        # 2. _client_body strips resume_of_run_id from untrusted client requests, keeps for service requests
        chat_req = ChatRequest(workspace_id="ws-1", messages=[], resume_of_run_id="run-old-1")
        fake_req = create_autospec(Request, instance=True)
        monkeypatch.setattr(messaging_mod, "_is_service_request", lambda _r: False)
        assert messaging_mod._client_body(chat_req, fake_req).resume_of_run_id is None
        monkeypatch.setattr(messaging_mod, "_is_service_request", lambda _r: True)
        assert messaging_mod._client_body(chat_req, fake_req).resume_of_run_id == "run-old-1"

        # 3. run_with_catalog_snapshot binds snapshot across async stream for MultimodalStrip + key_fingerprint check
        cat_v1 = _compile_test_catalog(
            {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "provider_variants": {
                        "openai": {
                            "corp-gw": {
                                "display_name": "Corp GW",
                                "sdk": "openai",
                                "access_type": "api_key",
                                "billing": "cash",
                                "base_url": "https://gw.example.com/v1",
                            }
                        }
                    },
                    "models": {
                        "corp-gw": [
                            {
                                "id": "corp-m1",
                                "pricing": {"input": 1.0, "output": 2.0, "unit": "per_1m_tokens"},
                            }
                        ]
                    },
                },
                "models": {
                    "corp-m1": {
                        "model_id": "corp-m1",
                        "provider": "corp-gw",
                        "display_name": "Corp M1",
                        "visible": True,
                        "input_modalities": ["text", "image"],
                    }
                },
            },
            revision=1,
        )
        snap_v1 = build_effective_catalog_snapshot(
            ["corp-m1"],
            compiled=cat_v1,
            variant_credentials={
                "corp-gw": {
                    "bound_base_url": "https://gw.example.com/v1",
                    "canonical_bound_url": "https://gw.example.com:443/v1",
                    "key_fingerprint": make_key_fingerprint("sk-original-key-1111"),
                }
            },
        )

        # Mutate live catalog & rotate DB key after snapshot
        cat_v2 = _compile_test_catalog(
            {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "provider_variants": {
                        "openai": {
                            "corp-gw": {
                                "display_name": "Corp GW",
                                "sdk": "openai",
                                "access_type": "api_key",
                                "billing": "cash",
                                "base_url": "https://gw.example.com/v1",
                            }
                        }
                    },
                    "models": {
                        "corp-gw": [
                            {
                                "id": "corp-m1",
                                "pricing": {"input": 50.0, "output": 100.0, "unit": "per_1m_tokens"},
                            }
                        ]
                    },
                },
                "models": {
                    "corp-m1": {
                        "model_id": "corp-m1",
                        "provider": "corp-gw",
                        "display_name": "Corp M1",
                        "visible": True,
                        "input_modalities": ["text"],
                    }
                },
            },
            revision=2,
        )
        monkeypatch.setattr(LLM, "get_model_config", classmethod(lambda cls: cat_v2.model_config))
        monkeypatch.setattr(
            "src.dalton.database.team_model_catalog.get_catalog_provider_credential_sync",
            lambda slug, decrypt=True: {
                "provider_slug": slug,
                "api_key": "sk-rotated-key-9999",
                "bound_base_url": "https://gw.example.com/v1",
                "canonical_bound_url": "https://gw.example.com:443/v1",
                "key_fingerprint": make_key_fingerprint("sk-rotated-key-9999"),
            },
        )

        observed_in_stream: dict[str, Any] = {}

        async def _inner_stream():
            observed_in_stream["active"] = get_active_catalog_snapshot()
            strip_mw = MultimodalStripMiddleware(model_name="corp-m1")

            class _FakeClient:
                metadata = {"manifest_model": "corp-m1"}

            _, mods = strip_mw._target(_FakeClient())
            observed_in_stream["mods"] = mods
            with pytest.raises(CatalogVariantCredentialRevokedError):
                resolve_catalog_variant_binding_sync("corp-gw")
            yield "ok"

        async def _consume() -> list[str]:
            out = []
            async for item in run_with_catalog_snapshot(snap_v1, _inner_stream()):
                out.append(item)
            return out

        assert asyncio.run(_consume()) == ["ok"]
        assert observed_in_stream["active"] == snap_v1
        assert observed_in_stream["mods"] == ["text", "image"]
        assert get_active_catalog_snapshot() is None

        # 4. RunCoordinator._build_usage_writer binds handle.catalog_snapshot during settlement
        captured_costs: list[float] = []

        async def _fake_persist(self, **_kw: Any) -> bool:
            captured_costs.append(float((self._token_usage or {}).get("total_cost") or 0.0))
            return True

        from src.server.services.persistence.usage import UsagePersistenceService

        monkeypatch.setattr(
            UsagePersistenceService,
            "persist_usage",
            create_autospec(UsagePersistenceService.persist_usage, side_effect=_fake_persist),
        )
        coord = RunCoordinator()
        handle = RunHandle(
            run_id="run-1",
            thread_id="thread-1",
            turn_index=0,
            attempt_no=1,
            workspace_id="ws-1",
            user_id="u-1",
            recorded_as="u-1",
            catalog_snapshot=snap_v1,
        )
        outcome = RunOutcome(
            status="completed",
            per_call_records=[
                {
                    "model_name": "corp-m1",
                    "pricing_model_id": "corp-m1",
                    "pricing_provider": "corp-gw",
                    "billing_type": "platform",
                    "usage": {
                        "input_tokens": 1_000_000,
                        "output_tokens": 1_000_000,
                    },
                }
            ],
        )
        writer = coord._build_usage_writer(handle, outcome)
        asyncio.run(writer(None, "completed"))
        # Must settle using snap_v1 ($1 + $2 = $3), NOT live cat_v2 ($50 + $100 = $150)
        assert captured_costs == [pytest.approx(3.0, rel=1e-6)]

        # 5. SubagentRunCoordinator resume chains predecessor_run_id without inheriting stale snapshot
        #    (under r7 compare-and-reject, verified resumes record a fresh live snapshot at START).
        recorded_sub_kwargs: dict[str, Any] = {}

        async def _fake_start_task_run(**kw: Any) -> dict[str, Any]:
            recorded_sub_kwargs.update(kw)
            return {"task_run_id": kw["task_run_id"]}

        sub_coord = SubagentRunCoordinator("thread-1")
        monkeypatch.setattr(
            sub_coord,
            "get_latest_run",
            create_autospec(
                sub_coord.get_latest_run,
                return_value={
                    "task_run_id": "prev-task-run-1",
                    "final_checkpoint_id": "cp-1",
                    "replay_facts": {"effective_catalog_snapshot": snap_v1},
                },
            ),
        )
        monkeypatch.setattr(
            sr_db,
            "start_task_run",
            create_autospec(sr_db.start_task_run, side_effect=_fake_start_task_run),
        )
        monkeypatch.setattr(
            sub_coord,
            "_append_v2_frame",
            create_autospec(sub_coord._append_v2_frame, return_value=None),
        )
        from src.server.services import thread_control_stream

        monkeypatch.setattr(
            thread_control_stream,
            "announce_task_run_started",
            create_autospec(
                thread_control_stream.announce_task_run_started, return_value=None
            ),
        )
        asyncio.run(sub_coord.start_task_run(task_id="t1", cause="resume"))
        assert recorded_sub_kwargs["predecessor_run_id"] == "prev-task-run-1"
        assert recorded_sub_kwargs["start_checkpoint_id"] == "cp-1"
        assert "catalog_snapshot" not in recorded_sub_kwargs

    def test_r2_3_retire_and_rollback_preserve_automation_audit_history_and_canonical_lock_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.company_automations import validate_existing_history_state
        from src.dalton.services.team_model_catalog import service as tmc_service
        from src.server.database import automation as auto_db
        from src.server.database import user as user_db
        from src.server.database import user_subagents as us_db
        from src.server.services.automations import lifecycle as auto_lifecycle

        lock_events: list[str] = []
        auto_id = "33333333-3333-4333-8333-333333333333"
        stored_auto: dict[str, Any] = {
            "automation_id": auto_id,
            "user_id": "u-1",
            "name": "Earnings Watch",
            "description": None,
            "trigger_type": "cron",
            "cron_expression": "0 8 * * *",
            "timezone": "UTC",
            "trigger_config": {},
            "next_run_at": None,
            "last_run_at": None,
            "instruction": "Check earnings",
            "workspace_id": "ws-1",
            "llm_model": "model-a",
            "reasoning_effort": None,
            "additional_context": None,
            "thread_strategy": "new",
            "conversation_thread_id": None,
            "status": "active",
            "max_failures": 3,
            "failure_count": 0,
            "disable_reason": None,
            "delivery_config": {},
            "metadata": {},
            "scope": "personal",
            "notify_storage_mode": "task_row",
            "config_version": 1,
            "active_version": 1,
            "created_by": "u-1",
            "updated_by": "u-1",
            "audit_history": [],
            "created_at": "2026-11-20T00:00:00+00:00",
            "updated_at": "2026-11-20T00:00:00+00:00",
        }
        stored_pref: dict[str, Any] = {
            "user_id": "u-1",
            "model_preference": {"preferred_model": "model-a"},
            "agent_preference": {},
            "updated_at": "2026-11-20T00:00:00+00:00",
        }

        class _FakeCursor:
            def __init__(self) -> None:
                self._result: Any = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args: Any) -> bool:
                return False

            async def execute(self, sql: str, params: Any = None) -> None:
                s = " ".join(sql.split())
                if s.startswith("UPDATE user_preferences"):
                    lock_events.append("update_user_preferences")
                    stored_pref["model_preference"] = params[0].obj
                    stored_pref["agent_preference"] = params[1].obj
                    stored_pref["updated_at"] = "2026-11-25T01:00:00+00:00"
                    self._result = {"updated_at": stored_pref["updated_at"]}
                elif "FROM automations" in s and "FOR UPDATE" in s:
                    lock_events.append("lock_automation_row")
                    self._result = dict(stored_auto)
                elif s.startswith("UPDATE automations"):
                    lock_events.append("update_automation")
                    stored_auto["llm_model"] = params[0]
                    stored_auto["reasoning_effort"] = params[1]
                    stored_auto["config_version"] = params[2]
                    stored_auto["active_version"] = params[3]
                    stored_auto["updated_by"] = params[4]
                    stored_auto["audit_history"] = params[5].obj
                    stored_auto["updated_at"] = "2026-11-25T01:00:01+00:00"
                    self._result = {
                        "config_version": stored_auto["config_version"],
                        "active_version": stored_auto["active_version"],
                        "updated_at": stored_auto["updated_at"],
                    }
                else:
                    self._result = None

            async def fetchone(self) -> Any:
                return self._result

        class _FakeConn:
            @asynccontextmanager
            async def transaction(self):
                yield

            def cursor(self, **_kw: Any) -> _FakeCursor:
                return _FakeCursor()

        async def _fake_lock_prefs(uid: str, *, conn: Any) -> dict[str, Any]:
            lock_events.append(f"lock_user_preferences:{uid}")
            return dict(stored_pref)

        async def _fake_lock_autos(uid: str, *, conn: Any) -> None:
            lock_events.append(f"lock_user_automations:{uid}")

        async def _fake_lock_subs(uid: str, *, conn: Any) -> None:
            lock_events.append(f"lock_user_subagents:{uid}")

        monkeypatch.setattr(
            user_db,
            "lock_user_preferences",
            create_autospec(user_db.lock_user_preferences, side_effect=_fake_lock_prefs),
        )
        monkeypatch.setattr(
            auto_db,
            "lock_user_automations",
            create_autospec(auto_db.lock_user_automations, side_effect=_fake_lock_autos),
        )
        monkeypatch.setattr(
            us_db,
            "lock_user_subagents",
            create_autospec(us_db.lock_user_subagents, side_effect=_fake_lock_subs),
        )
        monkeypatch.setattr(
            auto_lifecycle,
            "check_claude_code_hosted",
            create_autospec(auto_lifecycle.check_claude_code_hosted, return_value=None),
        )
        monkeypatch.setattr(
            tmc_service,
            "_validate_successor_for_owner",
            create_autospec(tmc_service._validate_successor_for_owner, return_value=None),
        )

        post_cat = _compile_test_catalog(
            {"schema": CATALOG_SCHEMA, "retired": [{"key": "model-a", "provider": "openai"}]},
            revision=2,
        )
        refs = {
            "agent_config": {},
            "dalton_settings": {},
            "bundled_plugin_subagents": {},
            "plugin_subagents": [],
            "user_subagents": [],
            "automations": [
                {
                    "automation_id": auto_id,
                    "user_id": "u-1",
                    "workspace_id": "ws-1",
                    "llm_model": "model-a",
                    "reasoning_effort": None,
                    "config_version": 1,
                }
            ],
            "user_preferences": [
                {
                    "user_id": "u-1",
                    "slots": ["model_preference.preferred_model"],
                    "model_preference": {"preferred_model": "model-a"},
                    "agent_preference": {},
                    "updated_at": stored_pref["updated_at"],
                }
            ],
        }

        side_effects, _ = asyncio.run(
            tmc_service._validate_and_replace_references_in_tx(
                _FakeConn(),
                retired_key="model-a",
                successor_model="model-b",
                refs=refs,
                post_compiled=post_cat,
                migrate_user_defaults=True,
                actor_user_id="admin-1",
            )
        )

        # Canonical lock order: user_preferences MUST precede automations
        assert lock_events.index("lock_user_preferences:u-1") < lock_events.index(
            "lock_user_automations:u-1"
        )
        assert lock_events.index("update_user_preferences") < lock_events.index(
            "lock_automation_row"
        )

        # Automation history and active_version remain valid for subsequent normal edits/pauses
        assert stored_auto["llm_model"] == "model-b"
        assert stored_auto["config_version"] == 2
        assert stored_auto["active_version"] == 2
        assert len(stored_auto["audit_history"]) == 2
        validate_existing_history_state(stored_auto)

        # Now run rollback_catalog back to rev 0 and verify lock order + valid v3 audit_history
        lock_events.clear()

        @asynccontextmanager
        async def _fake_get_db(_conn=None):
            yield _FakeConn()

        monkeypatch.setattr(tmc_service, "get_db_connection", _fake_get_db)
        monkeypatch.setattr(
            tmc_service,
            "acquire_catalog_exclusive_lock",
            create_autospec(tmc_service.acquire_catalog_exclusive_lock, return_value=None),
        )
        monkeypatch.setattr(
            tmc_service,
            "get_catalog_row",
            create_autospec(
                tmc_service.get_catalog_row,
                return_value={
                    "revision": 2,
                    "payload": {"schema": CATALOG_SCHEMA, "retired": [{"key": "model-a"}]},
                    "payload_sha256": "sha2",
                    "legacy_sync_import_status": "not_applicable",
                    "last_known_legacy_retired": [],
                },
            ),
        )
        monkeypatch.setattr(
            tmc_service,
            "get_catalog_revision",
            create_autospec(
                tmc_service.get_catalog_revision,
                return_value={"revision": 0, "payload": {"schema": CATALOG_SCHEMA}},
            ),
        )
        monkeypatch.setattr(
            tmc_service,
            "list_catalog_provider_credentials",
            create_autospec(tmc_service.list_catalog_provider_credentials, return_value={}),
        )
        monkeypatch.setattr(
            tmc_service,
            "list_revisions_between",
            create_autospec(
                tmc_service.list_revisions_between,
                return_value=[{"revision": 2, "side_effects": side_effects}],
            ),
        )
        monkeypatch.setattr(
            tmc_service,
            "save_catalog_revision",
            create_autospec(
                tmc_service.save_catalog_revision,
                return_value={
                    "catalog": {"revision": 3},
                    "revision_record": {"revision": 3, "action": "rollback"},
                },
            ),
        )
        monkeypatch.setattr(
            tmc_service,
            "publish_catalog_changed",
            create_autospec(tmc_service.publish_catalog_changed, return_value=None),
        )
        monkeypatch.setattr(
            tmc_service,
            "invalidate_catalog_redis_caches",
            create_autospec(tmc_service.invalidate_catalog_redis_caches, return_value=None),
        )
        monkeypatch.setattr(
            user_db,
            "invalidate_user_prefs_cache",
            create_autospec(user_db.invalidate_user_prefs_cache, return_value=None),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr("src.dalton.llm.overlay._is_native_oauth_sync_enabled", lambda: False)

        asyncio.run(
            rollback_catalog(
                actor_user_id="admin-1",
                data=CatalogRollbackInput(target_revision=0, expected_revision=2),
            )
        )
        assert lock_events.index("lock_user_preferences:u-1") < lock_events.index(
            "lock_user_automations:u-1"
        )
        assert stored_auto["llm_model"] == "model-a"
        assert stored_auto["config_version"] == 3
        assert stored_auto["active_version"] == 3
        assert len(stored_auto["audit_history"]) == 3
        validate_existing_history_state(stored_auto)

    def test_r2_4_in_lock_subagent_and_rollback_deleted_model_checks_and_per_owner_successor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.team_model_catalog import service as tmc_service
        from src.server.services.account import OwnAccess
        from src.server.services.policy import Policy
        from src.server.services.subagents.rules import SubagentInvalid
        from src.server.services.subagents.writes import PreparedSave, Subagents, apply_save

        cat_live = _compile_test_catalog({"schema": CATALOG_SCHEMA}, revision=3)
        monkeypatch.setattr(
            tmc_service,
            "get_compiled_catalog_async",
            create_autospec(tmc_service.get_compiled_catalog_async, return_value=cat_live),
        )

        # 1. Rollback-deleted team model (not in retired list, absent from active catalog) -> 400 model_unavailable
        with pytest.raises(HTTPException) as exc_unavail:
            asyncio.run(assert_model_not_retired_under_lock(object(), "team-rolled-back-model"))
        assert exc_unavail.value.status_code == 400
        assert exc_unavail.value.detail["code"] == "model_unavailable"

        # Active model succeeds; personal custom model passes only when allow_custom=True & in pref
        assert asyncio.run(assert_model_not_retired_under_lock(object(), "gpt-test")) is cat_live
        assert (
            asyncio.run(
                assert_model_not_retired_under_lock(
                    object(),
                    "my-custom-1",
                    model_pref={"custom_models": [{"name": "my-custom-1", "provider": "openai"}]},
                    allow_custom=True,
                )
            )
            is cat_live
        )

        # 2. subagents/writes.py apply_save rejects model retired/removed after prepare_save (under lock)
        prepared = PreparedSave(
            user_id="u-1",
            name="my-agent",
            version=None,
            kind="custom",
            patch={
                "description": "Custom analyst",
                "instructions": "Analyze",
                "model": "team-rolled-back-model",
            },
        )
        from src.server.services import account as account_mod
        from src.server.services.llm import byok as byok_mod
        from src.server.services.llm import user_models as user_models_mod

        found = Subagents.of({}, (), [])
        monkeypatch.setattr(
            user_models_mod,
            "get_model_preference",
            create_autospec(user_models_mod.get_model_preference, return_value={}),
        )
        with pytest.raises(SubagentInvalid) as sub_exc:
            asyncio.run(apply_save(prepared, found, object(), updated_by="user"))
        assert any(e.field == "model" for e in sub_exc.value.errors)

        # 3. _validate_successor_for_owner enforces per-owner account policy & credential availability
        # 3a. Account policy forbids successor_model
        restricted_policy = create_autospec(Policy, instance=True)
        restricted_policy.allows_model.return_value = False
        restricted_policy.allows.return_value = True
        orig_own_access = account_mod.own_access
        monkeypatch.setattr(
            account_mod,
            "own_access",
            create_autospec(
                orig_own_access,
                return_value=OwnAccess(
                    policy=restricted_policy, api_keys=True, subscriptions=None
                ),
            ),
        )
        with pytest.raises(CatalogConflictError) as pol_exc:
            asyncio.run(
                tmc_service._validate_successor_for_owner(
                    object(),
                    owner_id="u-restricted",
                    successor="model-b",
                    post_compiled=cat_live,
                )
            )
        assert pol_exc.value.code == "successor_not_allowed_for_owner"

        # 3b. Account policy allows model, but owner has neither platform key nor BYOK key
        open_policy = create_autospec(Policy, instance=True)
        open_policy.allows_model.return_value = True
        open_policy.allows.return_value = True
        monkeypatch.setattr(
            account_mod,
            "own_access",
            create_autospec(
                orig_own_access,
                return_value=OwnAccess(
                    policy=open_policy, api_keys=True, subscriptions=None
                ),
            ),
        )
        monkeypatch.setattr("src.llms.llm.platform_key_configured", lambda _m: False)
        monkeypatch.setattr(
            byok_mod,
            "walk_byok_candidates",
            create_autospec(byok_mod.walk_byok_candidates, return_value=(None, None)),
        )
        with pytest.raises(CatalogConflictError) as cred_exc:
            asyncio.run(
                tmc_service._validate_successor_for_owner(
                    object(),
                    owner_id="u-no-key",
                    successor="model-b",
                    post_compiled=cat_live,
                )
            )
        assert cred_exc.value.code == "successor_credential_unavailable"

    def test_r2_5_atomic_catalog_compile_revision_and_list_models_header_consistency(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import Response

        from src.dalton.database import team_model_catalog as tmc_db
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.constants import CATALOG_REVISION_HEADER
        from src.server.app import api_keys as api_keys_mod

        invalidate_compiled_catalog_cache()
        monkeypatch.setattr(
            compiled_mod,
            "_load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr("src.dalton.llm.overlay._is_native_oauth_sync_enabled", lambda: False)

        # Simulate concurrent commit advancing revision 1 -> 2 between head read and full row read
        payload_r2 = {
            "schema": CATALOG_SCHEMA,
            "models": {
                "r2-only-model": {
                    "model_id": "r2-only-model",
                    "provider": "openai",
                    "display_name": "R2 Only Model",
                    "visible": True,
                }
            },
        }
        monkeypatch.setattr(
            tmc_db,
            "get_catalog_head",
            create_autospec(
                tmc_db.get_catalog_head,
                return_value={
                    "revision": 1,
                    "payload_sha256": "sha1",
                    "legacy_sync_import_status": "not_applicable",
                    "last_known_legacy_retired": [],
                },
            ),
        )
        monkeypatch.setattr(
            tmc_db,
            "get_catalog_row",
            create_autospec(
                tmc_db.get_catalog_row,
                return_value={
                    "revision": 2,
                    "payload": payload_r2,
                    "payload_sha256": "sha2",
                    "legacy_sync_import_status": "not_applicable",
                    "last_known_legacy_retired": [],
                },
            ),
        )

        compiled = asyncio.run(get_compiled_catalog_async())
        # Must compile & cache under row's ACTUAL revision (2), NEVER stale head revision (1)
        assert compiled.revision == 2
        assert "r2-only-model" in compiled.model_config.llm_config
        with compiled_mod._CACHE_LOCK:
            cached_revs = {k[0] for k in compiled_mod._COMPILED_BY_REVISION}
        assert 2 in cached_revs
        assert 1 not in cached_revs

        # GET /api/v1/models must build response models from the SAME compiled_catalog.model_config as header
        cat_rev5 = _compile_test_catalog(
            {
                "schema": CATALOG_SCHEMA,
                "models": {
                    "rev5-model": {
                        "model_id": "rev5-model",
                        "provider": "openai",
                        "display_name": "Rev 5 Model",
                        "visible": True,
                    }
                },
            },
            revision=5,
        )
        cat_rev6 = _compile_test_catalog(
            {
                "schema": CATALOG_SCHEMA,
                "models": {
                    "rev6-drift-model": {
                        "model_id": "rev6-drift-model",
                        "provider": "openai",
                        "display_name": "Rev 6 Drift",
                        "visible": True,
                    }
                },
            },
            revision=6,
        )
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_async",
            create_autospec(compiled_mod.get_compiled_catalog_async, return_value=cat_rev5),
        )
        # Even if global LLM.get_model_config() returns rev6 due to concurrent worker update,
        # list_models must strictly use compiled_catalog.model_config (rev5)!
        monkeypatch.setattr(LLM, "get_model_config", classmethod(lambda cls: cat_rev6.model_config))
        resp = Response()
        body = asyncio.run(api_keys_mod.list_models(resp))
        assert resp.headers[CATALOG_REVISION_HEADER] == "5"
        assert body["catalog_revision"] == 5
        openai_models = body["models"]["openai"]["models"]
        assert "rev5-model" in openai_models
        assert "rev6-drift-model" not in openai_models

    def test_r2_6_provider_models_single_credential_read_blocks_rotated_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.database import team_model_catalog as tmc_db
        from src.dalton.services.team_model_catalog import transport as tmc_transport
        from src.server.services.llm.provider_models import service as pm_service

        cat = _compile_test_catalog(
            {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "provider_variants": {
                        "openai": {
                            "corp-gw": {
                                "display_name": "Corp GW",
                                "sdk": "openai",
                                "access_type": "api_key",
                                "billing": "cash",
                                "base_url": "https://gw.example.com/v1",
                            }
                        }
                    }
                },
            },
            revision=1,
        )
        monkeypatch.setattr(LLM, "get_model_config", classmethod(lambda cls: cat.model_config))

        old_key = "sk-old-discovery-key-1111"
        old_fp = make_key_fingerprint(old_key)
        cred_calls: list[bool] = []

        async def _fake_get_cred(slug: str, *, conn: Any = None, decrypt: bool = True):
            cred_calls.append(decrypt)
            # First call returns old key; any second call would return a rotated key!
            if len(cred_calls) == 1:
                return {
                    "provider_slug": slug,
                    "api_key": old_key,
                    "bound_base_url": "https://gw.example.com/v1",
                    "canonical_bound_url": "https://gw.example.com:443/v1",
                    "key_fingerprint": old_fp,
                }
            return {
                "provider_slug": slug,
                "api_key": "sk-rotated-9999",
                "bound_base_url": "https://gw.example.com/v1",
                "canonical_bound_url": "https://gw.example.com:443/v1",
                "key_fingerprint": make_key_fingerprint("sk-rotated-9999"),
            }

        monkeypatch.setattr(
            tmc_db,
            "get_catalog_provider_credential",
            create_autospec(tmc_db.get_catalog_provider_credential, side_effect=_fake_get_cred),
        )
        monkeypatch.setattr(
            pm_service,
            "_cache_read",
            create_autospec(pm_service._cache_read, return_value=None),
        )
        monkeypatch.setattr(
            pm_service,
            "_cache_write",
            create_autospec(pm_service._cache_write, return_value=None),
        )
        monkeypatch.setattr(
            pm_service,
            "_gateway_catalog",
            create_autospec(pm_service._gateway_catalog, return_value=None),
        )

        transport_args: dict[str, Any] = {}

        def _fake_make_transport(**kw: Any):
            transport_args.update(kw)
            return object()

        monkeypatch.setattr(
            tmc_transport,
            "make_catalog_variant_http_transport",
            create_autospec(
                tmc_transport.make_catalog_variant_http_transport,
                side_effect=_fake_make_transport,
            ),
        )
        monkeypatch.setattr(
            pm_service,
            "fetch_list",
            create_autospec(pm_service.fetch_list, return_value={"data": []}),
        )

        asyncio.run(pm_service.available_models("u-1", "corp-gw", refresh=True))
        # Must read DB credential ONCE (decrypt=True) and bind exact matching fingerprint
        assert cred_calls == [True]
        assert transport_args["expected_key_fingerprint"] == old_fp
        assert transport_args["bound_base_url"] == "https://gw.example.com/v1"
        assert transport_args["verify_db_binding"] is True

    def test_r2_7_restore_factory_default_clears_pricing_overrides_and_subscription_variant_skips_parent_cash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.team_model_catalog import service as tmc_service
        from src.dalton.services.team_model_catalog.snapshot import lookup_snapshot_pricing

        # Part A: restore_factory_default_model clears providers.model_overrides[prov][card_id]
        saved_payloads: list[dict[str, Any]] = []

        class _FakeConn:
            @asynccontextmanager
            async def transaction(self):
                yield

        @asynccontextmanager
        async def _fake_get_db(_conn=None):
            yield _FakeConn()

        async def _fake_save(conn: Any, **kw: Any) -> dict[str, Any]:
            saved_payloads.append(kw["payload"])
            return {
                "catalog": {"revision": int(kw["expected_revision"]) + 1},
                "revision_record": {"revision": int(kw["expected_revision"]) + 1},
            }

        monkeypatch.setattr(tmc_service, "get_db_connection", _fake_get_db)
        monkeypatch.setattr(
            tmc_service,
            "acquire_catalog_exclusive_lock",
            create_autospec(tmc_service.acquire_catalog_exclusive_lock, return_value=None),
        )
        monkeypatch.setattr(
            tmc_service,
            "get_catalog_row",
            create_autospec(
                tmc_service.get_catalog_row,
                return_value={
                    "revision": 4,
                    "payload": {
                        "schema": CATALOG_SCHEMA,
                        "model_overrides": {"gpt-test": {"display_name": "Edited GPT"}},
                        "dalton_meta": {"gpt-test": {"family": "openai-gpt"}},
                        "task_costs": {"bounds": {"gpt-test": {"model": "model-a", "times": 1.0}}},
                        "providers": {
                            "model_overrides": {
                                "openai": {
                                    "gpt-test": {
                                        "pricing": {"input": 99.0, "output": 199.0, "unit": "per_1m_tokens"}
                                    }
                                }
                            }
                        },
                    },
                    "payload_sha256": "sha4",
                    "legacy_sync_import_status": "not_applicable",
                    "last_known_legacy_retired": [],
                },
            ),
        )
        monkeypatch.setattr(
            tmc_service,
            "save_catalog_revision",
            create_autospec(tmc_service.save_catalog_revision, side_effect=_fake_save),
        )
        monkeypatch.setattr(
            tmc_service,
            "publish_catalog_changed",
            create_autospec(tmc_service.publish_catalog_changed, return_value=None),
        )
        monkeypatch.setattr(
            tmc_service,
            "invalidate_catalog_redis_caches",
            create_autospec(tmc_service.invalidate_catalog_redis_caches, return_value=None),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr("src.dalton.llm.overlay._is_native_oauth_sync_enabled", lambda: False)

        asyncio.run(
            tmc_service.restore_factory_default_model(
                actor_user_id="admin-1",
                model_key="gpt-test",
                expected_revision=4,
            )
        )
        assert len(saved_payloads) == 1
        restored = saved_payloads[0]
        assert "gpt-test" not in (restored.get("model_overrides") or {})
        assert "openai" not in ((restored.get("providers") or {}).get("model_overrides") or {})
        restored_compiled = _compile_test_catalog(restored, revision=5)
        monkeypatch.setattr(
            LLM, "get_model_config", classmethod(lambda cls: restored_compiled.model_config)
        )
        restored_pricing = find_model_pricing("gpt-test", provider="openai")
        assert restored_pricing is not None
        assert restored_pricing["input"] == 2.0
        assert restored_pricing["output"] == 8.0

        # Part B: billing="subscription" catalog variant sharing parent model_id returns subscription marker, NOT parent cash card
        sub_variant_cat = _compile_test_catalog(
            {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "provider_variants": {
                        "openai": {
                            "acme-openai-sub": {
                                "display_name": "Acme OpenAI Seat",
                                "sdk": "openai",
                                "access_type": "api_key",
                                "billing": "subscription",
                                "base_url": "https://sub.example.com/v1",
                            }
                        }
                    }
                },
                "models": {
                    "acme-gpt-sub": {
                        "model_id": "gpt-test",
                        "provider": "acme-openai-sub",
                        "display_name": "Acme GPT Seat",
                        "visible": True,
                    }
                },
                "dalton_meta": {
                    "acme-gpt-sub": {
                        "billing": "subscription",
                        "notional_pricing": {"input": 1.0, "output": 4.0, "source_kind": "official"},
                    }
                },
            },
            revision=6,
        )
        monkeypatch.setattr(
            LLM, "get_model_config", classmethod(lambda cls: sub_variant_cat.model_config)
        )
        sub_pricing = find_model_pricing("gpt-test", provider="acme-openai-sub")
        assert sub_pricing == {"pricing_type": "subscription"}

        # Snapshot built for the subscription variant also avoids inheriting parent cash pricing
        snap_sub = build_effective_catalog_snapshot(
            ["acme-gpt-sub"],
            compiled=sub_variant_cat,
            variant_credentials={
                "acme-openai-sub": {
                    "bound_base_url": "https://sub.example.com/v1",
                    "canonical_bound_url": "https://sub.example.com:443/v1",
                    "key_fingerprint": make_key_fingerprint("sk-sub-1111"),
                }
            },
        )
        assert snap_sub["models"]["acme-gpt-sub"]["pricing"] is None
        hit, snap_p = lookup_snapshot_pricing(
            "acme-gpt-sub", "acme-openai-sub", model_id="gpt-test", snapshot=snap_sub
        )
        assert hit is True
        assert snap_p == {"pricing_type": "subscription"}


# ---------------------------------------------------------------------------
# Group 7: R4 regression tests covering all 10 R2 review fixes
# ---------------------------------------------------------------------------


class TestR4Fixes:
    def test_r4_1_snapshot_identity_keys_json_lossless_and_legacy_compat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.database import team_model_catalog as tmc_db
        from src.dalton.services.team_model_catalog.snapshot import (
            _identity_key,
            lookup_snapshot_pricing,
            serialize_catalog_snapshot,
        )
        from src.server.utils.pg_sanitize import SafeJson

        cat = _compile_test_catalog({"schema": CATALOG_SCHEMA}, revision=3)
        snap = build_effective_catalog_snapshot(["gpt-test"], compiled=cat)

        # 1. Identity key is JSON-lossless (no NUL byte, no \u0000 escape)
        expected_key = _identity_key("openai", "gpt-test")
        assert "\x00" not in expected_key
        assert expected_key in snap["by_identity"]

        serialized = serialize_catalog_snapshot(snap)
        safe_serialized = SafeJson(snap).dumps(snap)
        assert serialized == safe_serialized
        assert "\\u0000" not in serialized
        assert "\x00" not in serialized
        round_tripped = json.loads(serialized)
        assert expected_key in round_tripped["by_identity"]

        # 2. Backfill path binds via SafeJson (same serializer as first-write lifecycle/subagent_runs)
        captured_params: list[Any] = []

        class _FakeCur:
            rowcount = 1

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, sql: str, params: Any = None) -> None:
                captured_params.append(params)

        class _FakeConn:
            def cursor(self, *args, **kwargs):
                return _FakeCur()

        @asynccontextmanager
        async def _fake_get_db(conn=None):
            yield conn or _FakeConn()

        monkeypatch.setattr(tmc_db, "get_db_connection", _fake_get_db)
        wrote = asyncio.run(
            tmc_db.backfill_run_catalog_snapshot(
                "11111111-1111-4111-8111-111111111111", snap, is_subagent=False
            )
        )
        assert wrote is True
        assert len(captured_params) == 1
        assert isinstance(captured_params[0][1], SafeJson)
        assert captured_params[0][1].dumps(snap) == safe_serialized

        # 3. Reading remains backward-compatible with legacy NUL and SafeJson-stripped keys
        entry_val = dict(snap["by_identity"][expected_key])
        legacy_nul_snap = {
            "schema": snap["schema"],
            "catalog_revision": 3,
            "models": {},
            "by_identity": {"openai\x00gpt-test": entry_val},
            "variants": {},
        }
        legacy_stripped_snap = {
            "schema": snap["schema"],
            "catalog_revision": 3,
            "models": {},
            "by_identity": {"openaigpt-test": entry_val},
            "variants": {},
        }
        for legacy_snap in (legacy_nul_snap, legacy_stripped_snap):
            hit, pricing = lookup_snapshot_pricing(
                "gpt-test", "openai", model_id="gpt-test", snapshot=legacy_snap
            )
            assert hit is True
            assert pricing is not None and pricing["input"] == 2.0

    def test_r4_2_verify_resume_route_rejects_mutated_route_and_snapshot_pricing_checks_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.database import team_model_catalog as tmc_db
        from src.dalton.services.team_model_catalog.constants import (
            CATALOG_ROUTE_CHANGED_CODE,
            CATALOG_ROUTE_CHANGED_MESSAGE,
        )
        from src.dalton.services.team_model_catalog.snapshot import (
            lookup_snapshot_pricing,
            resolve_live_route_fingerprint,
            verify_resume_route,
        )
        from src.dalton.services.team_model_catalog.transport import (
            resolve_catalog_variant_binding_async,
        )
        from src.llms.llm import LLM

        # Revision 1: team-m1 routes to variant-a (model_id="m-a", input=$2)
        cat_a = _compile_test_catalog(
            {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "provider_variants": {
                        "openai": {
                            "variant-a": {
                                "display_name": "Variant A",
                                "sdk": "openai",
                                "access_type": "api_key",
                                "billing": "cash",
                                "base_url": "https://a.example.com/v1",
                            }
                        }
                    },
                    "models": {
                        "variant-a": [
                            {"id": "m-a", "pricing": {"input": 2.0, "output": 8.0}}
                        ]
                    },
                },
                "models": {
                    "team-m1": {
                        "model_id": "m-a",
                        "provider": "variant-a",
                        "display_name": "Team M1",
                        "visible": True,
                        "parameters": {"temperature": 0.2, "max_tokens": 4096},
                    }
                },
            },
            revision=1,
        )
        fp_a = make_key_fingerprint("sk-variant-a-1111")
        snap_a = build_effective_catalog_snapshot(
            ["team-m1"],
            compiled=cat_a,
            variant_credentials={
                "variant-a": {
                    "bound_base_url": "https://a.example.com/v1",
                    "canonical_bound_url": "https://a.example.com:443/v1",
                    "key_fingerprint": fp_a,
                }
            },
        )

        # Revision 2 (live catalog after interruption): team-m1 moved to variant-b (model_id="m-b")
        cat_b = _compile_test_catalog(
            {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "provider_variants": {
                        "openai": {
                            "variant-b": {
                                "display_name": "Variant B",
                                "sdk": "openai",
                                "access_type": "api_key",
                                "billing": "cash",
                                "base_url": "https://b.example.com/v1",
                            }
                        }
                    },
                    "models": {
                        "variant-b": [
                            {"id": "m-b", "pricing": {"input": 20.0, "output": 80.0}}
                        ]
                    },
                },
                "models": {
                    "team-m1": {
                        "model_id": "m-b",
                        "provider": "variant-b",
                        "display_name": "Team M1",
                        "visible": True,
                        "parameters": {"temperature": 0.9, "max_tokens": 8192},
                    }
                },
            },
            revision=2,
        )
        monkeypatch.setattr(LLM, "get_model_config", classmethod(lambda cls: cat_b.model_config))
        fp_b = make_key_fingerprint("sk-variant-b-2222")
        monkeypatch.setattr(
            tmc_db,
            "get_catalog_provider_credential",
            create_autospec(
                tmc_db.get_catalog_provider_credential,
                side_effect=lambda slug, conn=None, decrypt=True: (
                    {
                        "provider_slug": "variant-b",
                        "api_key": "sk-variant-b-2222",
                        "bound_base_url": "https://b.example.com/v1",
                        "canonical_bound_url": "https://b.example.com:443/v1",
                        "key_fingerprint": fp_b,
                    }
                    if slug == "variant-b"
                    else None
                ),
            ),
        )

        # 1. Under compare-and-reject, live resolution sees variant-b (no snapshot client overlay)
        #    and verify_resume_route refuses with 400 catalog_route_changed.
        live_route = asyncio.run(resolve_live_route_fingerprint("team-m1"))
        assert live_route.provider == "variant-b"
        assert live_route.model_id == "m-b"
        interrupted_row = {
            "conversation_response_id": "11111111-1111-4111-8111-111111111112",
            "conversation_thread_id": "thread-r4-2",
            "metadata": {
                "llm_model": "team-m1",
                "effective_catalog_snapshot": snap_a,
            },
        }
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                verify_resume_route(
                    "11111111-1111-4111-8111-111111111112",
                    live_route,
                    run_row=interrupted_row,
                )
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == CATALOG_ROUTE_CHANGED_CODE
        assert exc_info.value.detail["message"] == CATALOG_ROUTE_CHANGED_MESSAGE

        # Direct in-flight call attempting to bind variant-b for model_key="team-m1" under snap_a is still rejected
        with bind_catalog_snapshot(snap_a):
            with pytest.raises(CatalogVariantCredentialRevokedError):
                asyncio.run(
                    resolve_catalog_variant_binding_async("variant-b", model_key="team-m1")
                )

        # 2. Snapshot pricing lookup rejects mismatched (provider, model_id) even when model_key matches
        hit_wrong_prov, p_wrong_prov = lookup_snapshot_pricing(
            "team-m1", provider="variant-b", model_id="m-b", snapshot=snap_a
        )
        assert hit_wrong_prov is False and p_wrong_prov is None

        hit_wrong_id, p_wrong_id = lookup_snapshot_pricing(
            "team-m1", provider="variant-a", model_id="m-b", snapshot=snap_a
        )
        assert hit_wrong_id is False and p_wrong_id is None

        hit_ok, p_ok = lookup_snapshot_pricing(
            "team-m1", provider="variant-a", model_id="m-a", snapshot=snap_a
        )
        assert hit_ok is True and p_ok is not None and p_ok["input"] == 2.0

    def test_r4_3_subscription_variant_rejects_cash_pricing_and_settles_identically(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.team_model_catalog.validation import validate_model_fields
        from src.llms.llm import LLM
        from src.llms.pricing_utils import find_model_pricing
        from src.utils.tracking.core import calculate_cost_from_per_call_records

        # 1. Save-time validation rejects cash rates on a subscription provider
        with pytest.raises(CatalogValidationError) as exc_info:
            validate_model_fields(
                model_key="acme-sub-m1",
                model_entry={"provider": "acme-sub", "model_id": "m-sub"},
                pricing_card={"input": 2.0, "output": 8.0},
                dalton_meta_entry={
                    "billing": "subscription",
                    "notional_pricing": {"input": 2.0, "output": 8.0},
                },
                provider_info={"sdk": "openai", "billing": "subscription"},
                pricing_explicit=True,
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.code == "subscription_cash_pricing_forbidden"

        # 2. Even if a legacy payload contains a cash card under a subscription variant,
        # live lookup and snapshot lookup BOTH return subscription billing -> identical $0 cash settlement.
        cat = _compile_test_catalog(
            {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "provider_variants": {
                        "openai": {
                            "acme-sub": {
                                "display_name": "Acme Sub",
                                "sdk": "openai",
                                "access_type": "api_key",
                                "billing": "subscription",
                                "base_url": "https://sub.example.com/v1",
                            }
                        }
                    },
                    "models": {
                        "acme-sub": [
                            {"id": "m-sub", "pricing": {"input": 2.0, "output": 8.0}}
                        ]
                    },
                },
                "models": {
                    "acme-sub-m1": {
                        "model_id": "m-sub",
                        "provider": "acme-sub",
                        "display_name": "Acme Sub M1",
                        "visible": True,
                    }
                },
                "dalton_meta": {
                    "acme-sub-m1": {
                        "billing": "subscription",
                        "notional_pricing": {"input": 2.0, "output": 8.0},
                    }
                },
            },
            revision=7,
        )
        monkeypatch.setattr(LLM, "get_model_config", classmethod(lambda cls: cat.model_config))
        monkeypatch.setattr(
            "src.dalton.llm.notional_pricing.build_notional_price_index",
            lambda **kw: cat.notional_price_index,
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_dalton_meta",
            lambda *a, **kw: cat.dalton_meta,
        )

        assert find_model_pricing("m-sub", provider="acme-sub") == {"pricing_type": "subscription"}

        snap = build_effective_catalog_snapshot(
            ["acme-sub-m1"],
            compiled=cat,
            variant_credentials={
                "acme-sub": {
                    "bound_base_url": "https://sub.example.com/v1",
                    "canonical_bound_url": "https://sub.example.com:443/v1",
                    "key_fingerprint": make_key_fingerprint("sk-sub-9999"),
                }
            },
        )
        record = {
            "model_name": "acme-sub-m1",
            "usage": {
                "input_tokens": 1_000_000,
                "output_tokens": 100_000,
                "cached_tokens": 0,
                "cache_5m_tokens": 0,
                "cache_1h_tokens": 0,
            },
            "billing_type": "platform",
            "pricing_model_id": "m-sub",
            "pricing_provider": "acme-sub",
        }
        settled_live = calculate_cost_from_per_call_records([record])
        with bind_catalog_snapshot(snap):
            settled_snap = calculate_cost_from_per_call_records([record])

        assert settled_live["total_cost"] == 0.0
        assert settled_snap["total_cost"] == 0.0
        assert settled_live["total_cost"] == settled_snap["total_cost"]
        assert settled_live["notional_cost"] == pytest.approx(2.8)
        assert settled_snap["notional_cost"] == pytest.approx(2.8)

    def test_r4_4_cache_read_normalized_to_cached_input_for_flat_and_tiered_settlement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.team_model_catalog.service import _build_pricing_card_dict
        from src.dalton.services.team_model_catalog.validation import (
            CatalogModelUpsertInput,
            PricingCardInput,
            PricingTierInput,
        )
        from src.llms.llm import LLM
        from src.llms.pricing_utils import calculate_total_cost

        # 1. Flat card with ONLY cache_read (no cached_input)
        flat_input = CatalogModelUpsertInput(
            model_key="cache-flat",
            model_id="cache-flat",
            provider="openai",
            pricing=PricingCardInput(input=3.0, output=15.0, cache_read=0.3),
        )
        built_flat = _build_pricing_card_dict("cache-flat", "Cache Flat", flat_input)
        assert built_flat is not None
        assert "cache_read" not in built_flat["pricing"]
        assert built_flat["pricing"]["cached_input"] == 0.3

        # 2. Tiered card with ONLY per-tier cache_read
        tiered_input = CatalogModelUpsertInput(
            model_key="cache-tiered",
            model_id="cache-tiered",
            provider="openai",
            pricing=PricingCardInput(
                output=15.0,
                input_tiers=[
                    PricingTierInput(max_tokens=200_000, rate=3.0, cache_read=0.3),
                    PricingTierInput(max_tokens=None, rate=6.0, cache_read=0.6),
                ],
                input_pricing_mode="input_dependent",
            ),
        )
        built_tiered = _build_pricing_card_dict("cache-tiered", "Cache Tiered", tiered_input)
        assert built_tiered is not None
        assert built_tiered["pricing"]["input_tiers"][0]["cached_input"] == 0.3
        assert "cache_read" not in built_tiered["pricing"]["input_tiers"][0]
        assert built_tiered["pricing"]["input_tiers"][1]["cached_input"] == 0.6

        # 3. Compile directly from raw payload with cache_read (even without _build_pricing_card_dict)
        cat = _compile_test_catalog(
            {
                "schema": CATALOG_SCHEMA,
                "models": {
                    "cache-flat": {
                        "model_id": "cache-flat",
                        "provider": "openai",
                        "display_name": "Cache Flat",
                        "visible": True,
                    },
                    "cache-tiered": {
                        "model_id": "cache-tiered",
                        "provider": "openai",
                        "display_name": "Cache Tiered",
                        "visible": True,
                    },
                },
                "providers": {
                    "models": {
                        "openai": [
                            {
                                "id": "cache-flat",
                                "pricing": {"input": 3.0, "output": 15.0, "cache_read": 0.3},
                            },
                            {
                                "id": "cache-tiered",
                                "pricing": {
                                    "output": 15.0,
                                    "input_pricing_mode": "input_dependent",
                                    "input_tiers": [
                                        {"max_tokens": 200_000, "rate": 3.0, "cache_read": 0.3},
                                        {"max_tokens": None, "rate": 6.0, "cache_read": 0.6},
                                    ],
                                },
                            },
                        ]
                    }
                },
            },
            revision=8,
        )
        monkeypatch.setattr(LLM, "get_model_config", classmethod(lambda cls: cat.model_config))
        snap = build_effective_catalog_snapshot(["cache-flat", "cache-tiered"], compiled=cat)

        # 1M total input (500k cached + 500k fresh) + 0 output -> 0.5*3 + 0.5*0.3 = 1.65
        for ctx_cm in (bind_catalog_snapshot(None), bind_catalog_snapshot(snap)):
            with ctx_cm:
                res_flat = calculate_total_cost(
                    input_tokens=1_000_000,
                    cached_tokens=500_000,
                    output_tokens=0,
                    model_name="cache-flat",
                    provider="openai",
                )
                assert res_flat["breakdown"]["cached_input"]["cost"] == pytest.approx(0.15)
                assert res_flat["total_cost"] == pytest.approx(1.65)

                # Tiered (>200k prompt -> tier 1: rate 6.0, cached_input 0.6):
                # 250k total (100k cached + 150k fresh) -> 0.15*6.0 + 0.1*0.6 = 0.96
                res_tiered = calculate_total_cost(
                    input_tokens=250_000,
                    cached_tokens=100_000,
                    output_tokens=0,
                    model_name="cache-tiered",
                    provider="openai",
                )
                assert res_tiered["breakdown"]["cached_input"]["cost"] == pytest.approx(0.06)
                assert res_tiered["total_cost"] == pytest.approx(0.96)

    def test_r4_5_automation_rollback_and_recycle_restore_lock_and_reject_retired_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import datetime, timezone

        from src.dalton.database import automation_recycle as recycle_db
        from src.dalton.database import team_model_catalog as tmc_db
        from src.dalton.services import automation_recycle as recycle_svc
        from src.dalton.services.team_model_catalog import service as tmc_service

        events: list[str] = []
        cat_retired = _compile_test_catalog(
            {"schema": CATALOG_SCHEMA, "retired": [{"key": "model-a", "provider": "openai"}]},
            revision=4,
        )

        class _TxConn:
            @asynccontextmanager
            async def transaction(self):
                events.append("tx_enter")
                yield
                events.append("tx_exit")

            async def execute(self, sql: str, params: Any = None) -> None:
                events.append(f"exec:{sql[:28]}")

        @asynccontextmanager
        async def _fake_get_db(conn=None):
            yield _TxConn()

        async def _fake_shared_lock(conn: Any) -> None:
            events.append("catalog_shared_lock")

        async def _fake_lock_row(aid: str, *, owner_user_id: str | None, conn: Any):
            events.append("lock_automation")
            return current_row["row"]

        update_mock = create_autospec(recycle_db.update_automation_recycle, return_value=True)
        monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)
        monkeypatch.setattr(recycle_svc, "get_db_connection", _fake_get_db)
        monkeypatch.setattr(
            tmc_db,
            "acquire_catalog_shared_lock",
            create_autospec(tmc_db.acquire_catalog_shared_lock, side_effect=_fake_shared_lock),
        )
        monkeypatch.setattr(
            tmc_service,
            "get_compiled_catalog_async",
            create_autospec(tmc_service.get_compiled_catalog_async, return_value=cat_retired),
        )
        monkeypatch.setattr(
            recycle_db,
            "lock_automation_for_recycle",
            create_autospec(recycle_db.lock_automation_for_recycle, side_effect=_fake_lock_row),
        )
        monkeypatch.setattr(recycle_db, "update_automation_recycle", update_mock)

        # Case A: recycle bin restore of a soft-deleted automation pinned to retired model-a
        current_row: dict[str, Any] = {
            "row": {
                "automation_id": "22222222-2222-4222-8222-222222222222",
                "user_id": "u-1",
                "config_version": 2,
                "active_version": 2,
                "audit_history": [],
                "deleted_at": datetime(2026, 11, 25, tzinfo=timezone.utc),
                "scope": "personal",
                "status": "paused",
                "pre_delete_desired_status": "active",
                "trigger_type": "cron",
                "cron_expression": "0 9 * * *",
                "timezone": "UTC",
                "llm_model": "model-a",
                "file_name": "a.json",
            }
        }
        with pytest.raises(HTTPException) as restore_exc:
            asyncio.run(
                recycle_svc.restore_automation(
                    "22222222-2222-4222-8222-222222222222",
                    actor_user_id="u-1",
                    expected_config_version=2,
                    owner_user_id="u-1",
                    company=False,
                    prelocked_pref_row={},
                )
            )
        assert restore_exc.value.status_code == 400
        assert restore_exc.value.detail["code"] == "model_retired"
        assert events.index("catalog_shared_lock") < events.index("lock_automation")
        assert update_mock.call_count == 0

        # Case B: rollback to historical snapshot referencing retired model-a
        events.clear()
        snap_v1 = {
            "name": "Morning",
            "description": None,
            "trigger_type": "cron",
            "cron_expression": "0 9 * * *",
            "once_run_at": None,
            "timezone": "UTC",
            "instruction": "check",
            "workspace_id": None,
            "llm_model": "model-a",
            "reasoning_effort": None,
            "additional_context": None,
            "thread_strategy": "new",
            "conversation_thread_id": None,
            "max_failures": 3,
            "trigger_config": {},
            "delivery_config": {},
            "notify_enabled": False,
            "notify_routes": [],
            "desired_status": "active",
        }
        snap_v2 = {**snap_v1, "llm_model": "model-c"}
        current_row["row"] = {
            "automation_id": "22222222-2222-4222-8222-222222222222",
            "user_id": "u-1",
            "config_version": 2,
            "active_version": 2,
            "audit_history": [
                {"version": 1, "prev_version": None, "snapshot": snap_v1},
                {"version": 2, "prev_version": 1, "snapshot": snap_v2},
            ],
            "deleted_at": None,
            "scope": "personal",
            "status": "active",
            "trigger_type": "cron",
            "cron_expression": "0 9 * * *",
            "timezone": "UTC",
            "llm_model": "model-c",
            "file_name": "a.json",
        }
        with pytest.raises(HTTPException) as rb_exc:
            asyncio.run(
                recycle_svc.rollback_automation(
                    "22222222-2222-4222-8222-222222222222",
                    actor_user_id="u-1",
                    expected_config_version=2,
                    target_version=1,
                    owner_user_id="u-1",
                    company=False,
                    prelocked_pref_row={},
                )
            )
        assert rb_exc.value.status_code == 400
        assert rb_exc.value.detail["code"] == "model_retired"
        assert events.index("catalog_shared_lock") < events.index("lock_automation")
        assert update_mock.call_count == 0

    def test_r4_6_batch_retire_rejects_conflicting_clear_subagent_and_successor_400(
        self,
    ) -> None:
        from src.dalton.services.team_model_catalog.service import (
            _validate_and_replace_references_in_tx,
        )
        from src.dalton.services.team_model_catalog.validation import ModelRetireInput

        # 1. Pydantic input model raises 400 CatalogValidationError
        with pytest.raises(CatalogValidationError) as exc1:
            ModelRetireInput(clear_subagent_model=True, successor_model="gpt-test")
        assert exc1.value.status_code == 400
        assert exc1.value.code == "conflicting_retire_options"

        # 2. Service replacement guard raises the same 400 even if called directly
        cat = _compile_test_catalog({"schema": CATALOG_SCHEMA}, revision=2)
        refs = {
            "agent_config": {},
            "dalton_settings": {},
            "bundled_plugin_subagents": {},
            "plugin_subagents": [],
            "user_subagents": [
                {
                    "user_subagent_id": "33333333-3333-4333-8333-333333333333",
                    "user_id": "u-1",
                    "name": "researcher",
                    "model": "model-a",
                    "fields": {"model": "model-a"},
                    "updated_at": "2026-11-25T00:00:00Z",
                }
            ],
            "automations": [],
            "user_preferences": [],
        }
        with pytest.raises(CatalogValidationError) as exc2:
            asyncio.run(
                _validate_and_replace_references_in_tx(
                    object(),
                    retired_key="model-a",
                    successor_model="gpt-test",
                    refs=refs,
                    post_compiled=cat,
                    clear_subagent_model=True,
                )
            )
        assert exc2.value.status_code == 400
        assert exc2.value.code == "conflicting_retire_options"

    def test_r4_7_subagent_settings_and_platform_key_configured_use_variant_binding_no_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ptc_agent.agent.subagents.merge import BaseSubagent
        from src.dalton.database import team_model_catalog as tmc_db
        from src.llms.llm import LLM, platform_key_configured
        from src.server.app import subagents as subagents_router
        from src.server.database import user_subagents as us_db
        from src.server.services.subagents import context as sub_ctx_mod
        from src.server.services.subagents import writes as sub_writes_mod

        cat = _compile_test_catalog(
            {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "provider_variants": {
                        "openai": {
                            "corp-gw": {
                                "display_name": "Corp GW",
                                "sdk": "openai",
                                "access_type": "api_key",
                                "billing": "cash",
                                "base_url": "https://corp-llm.example.com/v1",
                            }
                        }
                    },
                    "models": {
                        "corp-gw": [
                            {"id": "corp-m1", "pricing": {"input": 1.0, "output": 3.0}}
                        ]
                    },
                },
                "models": {
                    "corp-model": {
                        "model_id": "corp-m1",
                        "provider": "corp-gw",
                        "display_name": "Corp Model",
                        "visible": True,
                    }
                },
            },
            revision=5,
        )
        monkeypatch.setattr(LLM, "get_model_config", classmethod(lambda cls: cat.model_config))

        # Bound credential present -> True without constructing LLM
        bound_row = {
            "provider_slug": "corp-gw",
            "bound_base_url": "https://corp-llm.example.com/v1",
            "canonical_bound_url": "https://corp-llm.example.com:443/v1",
            "key_fingerprint": make_key_fingerprint("sk-corp-1234"),
        }
        orig_get_cred_sync = tmc_db.get_catalog_provider_credential_sync
        orig_get_cred_async = tmc_db.get_catalog_provider_credential
        monkeypatch.setattr(
            tmc_db,
            "get_catalog_provider_credential_sync",
            create_autospec(orig_get_cred_sync, return_value=bound_row),
        )
        monkeypatch.setattr(
            tmc_db,
            "get_catalog_provider_credential",
            create_autospec(orig_get_cred_async, return_value=bound_row),
        )

        assert platform_key_configured("corp-model") is True

        # Missing credential -> False (never raises CatalogVariantSecurityError!)
        monkeypatch.setattr(
            tmc_db,
            "get_catalog_provider_credential_sync",
            create_autospec(orig_get_cred_sync, return_value=None),
        )
        assert platform_key_configured("corp-model") is False

        # SubagentContext.model_has_key & GET /subagents list endpoint succeed (200) after saving variant model
        from datetime import datetime, timezone

        from ptc_agent.agent.subagents.definition import SubagentDefinition

        async def _always_allow(_tier: int) -> bool:
            return True

        base = BaseSubagent(
            key="builtin:research",
            definition=SubagentDefinition(
                name="research",
                description="Research assistant",
                role_prompt="Research well.",
                tools=["web_search"],
            ),
        )
        ctx = sub_ctx_mod.SubagentContext(
            user_id="u-1",
            bases={"research": base},
            offered=("research",),
            model_pref={},
            is_byok=True,
            permits=_always_allow,
            search_provider="tavily",
            search_depth="basic",
            skills=frozenset(),
            connectors=(),
            brokerages_connected=False,
            oauth_accounts={},
        )
        assert asyncio.run(ctx.model_has_key("corp-model")) is True

        saved_rows = [
            {
                "name": "research",
                "kind": "override",
                "base_key": "builtin:research",
                "fields": {"model": "corp-model"},
                "updated_at": datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc),
            }
        ]
        monkeypatch.setattr(
            sub_writes_mod,
            "load_context",
            create_autospec(sub_writes_mod.load_context, return_value=ctx),
        )
        monkeypatch.setattr(
            us_db,
            "list_user_subagents",
            create_autospec(us_db.list_user_subagents, return_value=saved_rows),
        )
        monkeypatch.setattr(
            sub_writes_mod,
            "attach_titles",
            create_autospec(sub_writes_mod.attach_titles),
        )
        monkeypatch.setattr(
            ctx,
            "model_on_plan",
            create_autospec(ctx.model_on_plan, return_value=True),
        )

        resp = asyncio.run(subagents_router.get_subagents(user_id="u-1"))
        assert resp["subagents"]
        research_view = next(v for v in resp["subagents"] if v["name"] == "research")
        assert research_view["model"] == "corp-model"
        assert not any(
            p.get("code") == "model_unavailable" for p in research_view.get("problems") or []
        )

    def test_r4_8_chained_rollback_restores_different_preference_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.team_model_catalog import service as tmc_service
        from src.server.database import user as user_db

        # Hop 1 retires preferred_model: model-a -> model-c (T0 -> T1)
        # Hop 2 retires flash_model: model-b -> gpt-test (T1 -> T2) without user editing in between
        hop1 = {
            "kind": "user_preference",
            "id": "u-1",
            "user_id": "u-1",
            "before": {"model_preference.preferred_model": "model-a"},
            "after": {"model_preference.preferred_model": "model-c"},
            "before_updated_at": "2026-11-25T10:00:00Z",
            "after_updated_at": "2026-11-25T10:01:00Z",
        }
        hop2 = {
            "kind": "user_preference",
            "id": "u-1",
            "user_id": "u-1",
            "before": {"model_preference.flash_model": "model-b"},
            "after": {"model_preference.flash_model": "gpt-test"},
            "before_updated_at": "2026-11-25T10:01:00Z",
            "after_updated_at": "2026-11-25T10:02:00Z",
        }
        target, reason = resolve_chained_side_effect_target(
            [hop2, hop1],
            current_version="2026-11-25T10:02:00Z",
            current_value={
                "model_preference.preferred_model": "model-c",
                "model_preference.flash_model": "gpt-test",
            },
            version_kind="updated_at",
        )
        assert reason is None
        assert target == {
            "model_preference.preferred_model": "model-a",
            "model_preference.flash_model": "model-b",
        }

        # End-to-end rollback_catalog writes both fields and records actual_before / actual_after
        written_mp: list[dict[str, Any]] = []

        class _FakeCur:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, sql: str, params: Any = None) -> None:
                if "UPDATE user_preferences" in sql and params:
                    written_mp.append(dict(params[0].obj))

            async def fetchone(self):
                return {"updated_at": "2026-11-25T10:03:00Z"}

        class _FakeConn:
            @asynccontextmanager
            async def transaction(self):
                yield

            def cursor(self, *args, **kwargs):
                return _FakeCur()

        @asynccontextmanager
        async def _fake_get_db(_conn=None):
            yield _FakeConn()

        monkeypatch.setattr(tmc_service, "get_db_connection", _fake_get_db)
        monkeypatch.setattr(
            tmc_service,
            "acquire_catalog_exclusive_lock",
            create_autospec(tmc_service.acquire_catalog_exclusive_lock),
        )
        monkeypatch.setattr(
            tmc_service,
            "get_catalog_row",
            create_autospec(
                tmc_service.get_catalog_row,
                return_value={
                    "revision": 2,
                    "payload": {
                        "schema": CATALOG_SCHEMA,
                        "retired": [{"key": "model-a"}, {"key": "model-b"}],
                    },
                    "payload_sha256": "sha2",
                    "legacy_sync_import_status": "not_applicable",
                    "last_known_legacy_retired": [],
                },
            ),
        )
        monkeypatch.setattr(
            tmc_service,
            "get_catalog_revision",
            create_autospec(
                tmc_service.get_catalog_revision,
                return_value={"revision": 0, "payload": {"schema": CATALOG_SCHEMA}},
            ),
        )
        monkeypatch.setattr(
            tmc_service,
            "list_catalog_provider_credentials",
            create_autospec(tmc_service.list_catalog_provider_credentials, return_value={}),
        )
        monkeypatch.setattr(
            tmc_service,
            "list_revisions_between",
            create_autospec(
                tmc_service.list_revisions_between,
                return_value=[
                    {"revision": 1, "side_effects": [hop1]},
                    {"revision": 2, "side_effects": [hop2]},
                ],
            ),
        )
        monkeypatch.setattr(
            user_db,
            "lock_user_preferences",
            create_autospec(
                user_db.lock_user_preferences,
                return_value={
                    "user_id": "u-1",
                    "model_preference": {
                        "preferred_model": "model-c",
                        "flash_model": "gpt-test",
                    },
                    "agent_preference": {},
                    "updated_at": "2026-11-25T10:02:00Z",
                },
            ),
        )
        monkeypatch.setattr(
            user_db,
            "invalidate_user_prefs_cache",
            create_autospec(user_db.invalidate_user_prefs_cache),
        )
        monkeypatch.setattr(
            tmc_service,
            "save_catalog_revision",
            create_autospec(
                tmc_service.save_catalog_revision,
                return_value={
                    "catalog": {"revision": 3},
                    "revision_record": {"revision": 3},
                },
            ),
        )
        monkeypatch.setattr(
            tmc_service,
            "publish_catalog_changed",
            create_autospec(tmc_service.publish_catalog_changed),
        )
        monkeypatch.setattr(
            tmc_service,
            "invalidate_catalog_redis_caches",
            create_autospec(tmc_service.invalidate_catalog_redis_caches),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr("src.dalton.llm.overlay._is_native_oauth_sync_enabled", lambda: False)

        res = asyncio.run(
            rollback_catalog(
                actor_user_id="admin-1",
                data=CatalogRollbackInput(target_revision=0, expected_revision=2),
            )
        )
        assert len(written_mp) == 1
        assert written_mp[0]["preferred_model"] == "model-a"
        assert written_mp[0]["flash_model"] == "model-b"
        assert len(res["restored_tasks"]) == 1
        assert res["restored_tasks"][0]["before"] == {
            "model_preference.preferred_model": "model-c",
            "model_preference.flash_model": "gpt-test",
        }
        assert res["restored_tasks"][0]["after"] == {
            "model_preference.preferred_model": "model-a",
            "model_preference.flash_model": "model-b",
        }

    def test_r4_9_models_endpoint_pricing_and_metadata_pinned_to_compiled_catalog_revision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import Response

        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.llms.llm import LLM
        from src.server.app import api_keys as api_keys_router

        cat_v1 = _compile_test_catalog(
            {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "model_overrides": {
                        "openai": {"gpt-test": {"pricing": {"input": 2.0, "output": 8.0}}}
                    }
                },
            },
            revision=1,
        )
        cat_v2 = _compile_test_catalog(
            {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "model_overrides": {
                        "openai": {"gpt-test": {"pricing": {"input": 9.0, "output": 27.0}}}
                    }
                },
            },
            revision=2,
        )

        # Switch live global LLM.get_model_config() to v2 while calling metadata on v1
        monkeypatch.setattr(LLM, "get_model_config", classmethod(lambda cls: cat_v2.model_config))
        v1_meta = cat_v1.model_config.get_model_metadata()
        assert v1_meta["gpt-test"]["pricing"] == {"input": 2.0, "output": 8.0}

        # Also test GET /api/v1/models: header revision=1 must match body pricing (2.0 / 8.0)
        # even if global live catalog already flipped to revision=2 (9.0 / 27.0)
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_async",
            create_autospec(compiled_mod.get_compiled_catalog_async, return_value=cat_v1),
        )
        resp = Response()
        body = asyncio.run(api_keys_router.list_models(resp))
        assert resp.headers["X-Dalton-Catalog-Revision"] == "1"
        assert body["catalog_revision"] == 1
        assert body["model_metadata"]["gpt-test"]["pricing"] == {"input": 2.0, "output": 8.0}

    def test_r4_10_restore_factory_default_rejects_non_factory_key_400_without_partial_cleanup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.team_model_catalog import service as tmc_service
        from src.dalton.services.team_model_catalog.service import restore_factory_default_model

        class _FakeConn:
            @asynccontextmanager
            async def transaction(self):
                yield

        @asynccontextmanager
        async def _fake_get_db(_conn=None):
            yield _FakeConn()

        payload = {
            "schema": CATALOG_SCHEMA,
            "models": {
                "team-only-model": {
                    "model_id": "team-only-1",
                    "provider": "openai",
                    "display_name": "Team Only",
                    "visible": True,
                }
            },
            "dalton_meta": {"team-only-model": {"billing": "cash", "tool_calling": True}},
            "task_costs": {"bounds": {"team-only-model": {"model": "gpt-test", "times": 1.0}}},
        }
        save_mock = create_autospec(tmc_service.save_catalog_revision)
        monkeypatch.setattr(tmc_service, "get_db_connection", _fake_get_db)
        monkeypatch.setattr(
            tmc_service,
            "acquire_catalog_exclusive_lock",
            create_autospec(tmc_service.acquire_catalog_exclusive_lock),
        )
        monkeypatch.setattr(
            tmc_service,
            "get_catalog_row",
            create_autospec(
                tmc_service.get_catalog_row,
                return_value={
                    "revision": 3,
                    "payload": payload,
                    "payload_sha256": "sha3",
                    "legacy_sync_import_status": "not_applicable",
                    "last_known_legacy_retired": [],
                },
            ),
        )
        monkeypatch.setattr(tmc_service, "save_catalog_revision", save_mock)
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        with pytest.raises(CatalogValidationError) as exc_info:
            asyncio.run(
                restore_factory_default_model(
                    actor_user_id="admin-1",
                    model_key="team-only-model",
                    expected_revision=3,
                )
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.code == "not_a_factory_model"
        assert save_mock.call_count == 0
        # Original payload left completely untouched (no partial cleanup of dalton_meta or task_costs)
        assert "team-only-model" in payload["dalton_meta"]
        assert "team-only-model" in payload["task_costs"]["bounds"]


# ---------------------------------------------------------------------------
# Batch 1 r5: root-cause consolidation regressions (recovery entry + billing)
# ---------------------------------------------------------------------------


class TestR5Fixes:
    """Regressions for Batch 1 r5 root-cause consolidation fixes."""

    def test_r5_1_main_turn_resume_rejects_when_default_changes_a_to_b(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from ptc_agent.config import LLMConfig
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.dalton.services.team_model_catalog.constants import (
            CATALOG_ROUTE_CHANGED_CODE,
            CATALOG_ROUTE_CHANGED_MESSAGE,
        )
        from src.dalton.services.team_model_catalog.snapshot import (
            build_effective_catalog_snapshot,
            resolve_live_route_fingerprint,
            verify_resume_route,
        )
        from src.llms.llm import LLM as LLMFactory
        from src.server.services.llm.config import select_model

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )
        payload = {
            "schema": CATALOG_SCHEMA,
            "models": {
                "model-a": {
                    "model_id": "gpt-a",
                    "provider": "openai",
                    "display_name": "Model A",
                    "visible": True,
                    "parameters": {"max_tokens": 4096},
                },
                "model-b": {
                    "model_id": "gpt-b",
                    "provider": "openai",
                    "display_name": "Model B",
                    "visible": True,
                    "parameters": {"max_tokens": 8192},
                },
            },
            "providers": {
                "models": {
                    "openai": [
                        {"id": "gpt-a", "name": "A", "pricing": {"input": 1.0, "output": 2.0}},
                        {"id": "gpt-b", "name": "B", "pricing": {"input": 3.0, "output": 6.0}},
                    ]
                }
            },
            "dalton_meta": {
                "model-a": {"billing": "cash", "tool_calling": True},
                "model-b": {"billing": "cash", "tool_calling": True},
            },
        }
        compiled = compile_catalog_from_payload(payload, revision=20)
        cache_compiled_catalog(compiled)
        orig_get_compiled = compiled_mod.get_compiled_catalog_sync
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_compiled, return_value=compiled),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", compiled.model_config)

        # Original run ran on model-a (saved default at the time)
        snap = build_effective_catalog_snapshot(["model-a"], compiled=compiled)
        interrupted_row = {
            "conversation_response_id": "11111111-1111-1111-1111-111111111111",
            "conversation_thread_id": "thread-r5-1",
            "metadata": {
                "llm_model": "model-a",
                "effective_catalog_snapshot": snap,
            },
        }

        # Account default changes from model-a -> model-b before restart resume:
        # select_model resolves live default model-b, and verify_resume_route rejects with 400.
        cfg = SimpleNamespace(llm=LLMConfig(name="model-b"), llm_client=None)
        selection = select_model(
            cfg,
            {"preferred_model": "model-b"},
            slot="primary",
            request_model=None,
        )
        assert selection.effective_model == "model-b"
        live_route = asyncio.run(resolve_live_route_fingerprint(selection.effective_model))
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                verify_resume_route(
                    "11111111-1111-1111-1111-111111111111",
                    live_route,
                    thread_id="thread-r5-1",
                    run_row=interrupted_row,
                )
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == CATALOG_ROUTE_CHANGED_CODE
        assert exc_info.value.detail["message"] == CATALOG_ROUTE_CHANGED_MESSAGE

    def test_r5_2_subagent_resume_verifies_route_before_client_build_and_refuses_mutated_variant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import copy
        import inspect
        from types import SimpleNamespace

        from langchain_core.messages import ToolMessage

        from ptc_agent.agent.middleware.background_subagent import task_actions
        from ptc_agent.config.agent import TaskAsk
        from src.dalton.database import team_model_catalog as tmc_db
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.dalton.services.team_model_catalog.constants import (
            CATALOG_ROUTE_CHANGED_CODE,
            CATALOG_ROUTE_CHANGED_MESSAGE,
        )
        from src.dalton.services.team_model_catalog.snapshot import (
            build_effective_catalog_snapshot,
        )
        from src.llms.llm import LLM as LLMFactory

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        # Rev 1: team-shared points to variant A (fp-a)
        payload_rev1 = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "openai": {
                        "var-a": {
                            "parent_provider": "openai",
                            "display_name": "Variant A",
                            "sdk": "openai",
                            "billing": "cash",
                            "base_url": "https://var-a.example.com/v1",
                        },
                        "var-b": {
                            "parent_provider": "openai",
                            "display_name": "Variant B",
                            "sdk": "openai",
                            "billing": "cash",
                            "base_url": "https://var-b.example.com/v1",
                        },
                    }
                },
                "models": {
                    "var-a": [{"id": "m-a", "name": "MA", "pricing": {"input": 1.0, "output": 2.0}}],
                    "var-b": [{"id": "m-b", "name": "MB", "pricing": {"input": 3.0, "output": 6.0}}],
                },
            },
            "models": {
                "team-shared": {
                    "model_id": "m-a",
                    "provider": "var-a",
                    "display_name": "Team Shared",
                    "visible": True,
                }
            },
            "dalton_meta": {"team-shared": {"billing": "cash", "tool_calling": True}},
        }
        compiled_rev1 = compile_catalog_from_payload(payload_rev1, revision=30)
        snap_rev1 = build_effective_catalog_snapshot(
            ["team-shared"],
            compiled=compiled_rev1,
            variant_credentials={
                "var-a": {
                    "bound_base_url": "https://var-a.example.com/v1",
                    "canonical_bound_url": "https://var-a.example.com/v1",
                    "key_fingerprint": "fp-a",
                }
            },
        )

        # Rev 2: team-shared moves to variant B (fp-b)
        payload_rev2 = copy.deepcopy(payload_rev1)
        payload_rev2["models"]["team-shared"]["provider"] = "var-b"
        payload_rev2["models"]["team-shared"]["model_id"] = "m-b"
        compiled_rev2 = compile_catalog_from_payload(payload_rev2, revision=31)
        cache_compiled_catalog(compiled_rev2)
        orig_get_compiled = compiled_mod.get_compiled_catalog_sync
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_compiled, return_value=compiled_rev2),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", compiled_rev2.model_config)

        async def _fake_get_cred(
            slug: str, conn: Any = None, *, decrypt: bool = True
        ) -> dict[str, Any]:
            if slug == "var-b":
                return {
                    "provider_slug": "var-b",
                    "bound_base_url": "https://var-b.example.com/v1",
                    "canonical_bound_url": "https://var-b.example.com/v1",
                    "key_fingerprint": "fp-b",
                }
            return {
                "provider_slug": "var-a",
                "bound_base_url": "https://var-a.example.com/v1",
                "canonical_bound_url": "https://var-a.example.com/v1",
                "key_fingerprint": "fp-a",
            }

        monkeypatch.setattr(
            tmc_db,
            "get_catalog_provider_credential",
            create_autospec(
                tmc_db.get_catalog_provider_credential, side_effect=_fake_get_cred
            ),
        )

        # Prior subagent run row ran on team-shared under rev1 snapshot
        prior_subagent_row = {
            "task_run_id": "22222222-2222-2222-2222-222222222222",
            "thread_id": "thread-r5-2",
            "status": "completed",
            "replay_facts": {
                "model": "team-shared",
                "model_override": "team-shared",
                "effective_catalog_snapshot": snap_rev1,
            },
        }

        resolve_model_spy = create_autospec(task_actions._resolve_model)
        monkeypatch.setattr(task_actions, "_resolve_model", resolve_model_spy)
        monkeypatch.setattr(
            task_actions,
            "_read_latest_run",
            create_autospec(task_actions._read_latest_run, return_value=prior_subagent_row),
        )

        fake_task = SimpleNamespace(
            task_id="sub-1",
            display_id="Task-sub-1",
            subagent_type="research",
            is_pending=False,
            cancelled=False,
            terminal_status=None,
            asyncio_task=None,
            result=None,
            ask=TaskAsk(model="team-shared"),
            lock=asyncio.Lock(),
        )

        async def _resolve_task(*_a: Any, **_kw: Any) -> Any:
            return fake_task

        fake_mw = SimpleNamespace(
            _resolve_or_error=_resolve_task,
            subagent_types={"research"},
            _resume_claims=set(),
            registry=SimpleNamespace(
                get_by_id=lambda tid: fake_task if tid == "sub-1" else None,
                run_ledger=object(),
            ),
            task_models=SimpleNamespace(
                _own_model=lambda _st: "team-shared",
                _user_id="u-1",
                _model_pref={},
            ),
            namespace_owner=None,
        )
        fake_request = SimpleNamespace(
            tool_call={"id": "tc-sub-1", "args": {"action": "resume", "task_id": "sub-1"}},
            state={},
        )

        result = asyncio.run(
            task_actions._handle_resume(
                fake_mw,
                fake_request,
                handler=lambda _req: None,
                target_task_id="sub-1",
                subagent_type=None,
                description="resume sub-1",
                prompt="continue",
                args={"action": "resume", "task_id": "sub-1"},
                parent_thread_id="thread-r5-2",
                tool_call_id="tc-sub-1",
                current_run_id="parent-run-1",
                ask=TaskAsk(),
            )
        )
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert CATALOG_ROUTE_CHANGED_CODE in str(result.content)
        assert CATALOG_ROUTE_CHANGED_MESSAGE in str(result.content)
        # _resolve_model must NEVER be called when verify_resume_route rejects!
        assert resolve_model_spy.call_count == 0

        # Confirm task_actions._handle_resume calls verify_resume_route before _resolve_model
        src_text = inspect.getsource(task_actions._handle_resume)
        assert src_text.index("verify_resume_route") < src_text.index("_resolve_model")

    def test_r5_3_model_level_subscription_on_cash_provider_settles_identically_with_and_without_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.llm.notional_pricing import is_subscription_model
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.billing import resolve_model_billing
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.dalton.services.team_model_catalog.snapshot import (
            bind_catalog_snapshot,
            build_effective_catalog_snapshot,
        )
        from src.llms.llm import LLM as LLMFactory
        from src.llms.pricing_utils import find_model_pricing
        from src.utils.tracking.core import calculate_cost_from_per_call_records

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        # Cash-billed provider variant with parent="openai" + model reusing existing OpenAI cash model_id="gpt-test"
        # but marked model-level dalton_meta.billing="subscription".
        payload = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "openai": {
                        "cash-var-openai": {
                            "parent_provider": "openai",
                            "display_name": "Cash OpenAI Variant",
                            "sdk": "openai",
                            "billing": "cash",
                            "base_url": "https://cash-var.example.com/v1",
                        }
                    }
                }
            },
            "models": {
                "team-sub-gpt": {
                    "model_id": "gpt-test",
                    "provider": "cash-var-openai",
                    "display_name": "Team Sub GPT",
                    "visible": True,
                }
            },
            "dalton_meta": {
                "team-sub-gpt": {
                    "billing": "subscription",
                    "tool_calling": True,
                    "notional_pricing": {
                        "input": 4.0,
                        "output": 12.0,
                        "cached_input": 0.4,
                    },
                }
            },
        }
        compiled = compile_catalog_from_payload(payload, revision=40)
        cache_compiled_catalog(compiled)
        orig_get_compiled = compiled_mod.get_compiled_catalog_sync
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_compiled, return_value=compiled),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", compiled.model_config)
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_dalton_meta",
            lambda *a, **kw: compiled.dalton_meta,
        )

        assert (
            resolve_model_billing(
                model_key="team-sub-gpt",
                provider="cash-var-openai",
                model_id="gpt-test",
                model_config=compiled.model_config,
                dalton_meta=compiled.dalton_meta,
            )
            == "subscription"
        )
        assert (
            is_subscription_model(
                "team-sub-gpt",
                "platform",
                pricing_model_id="gpt-test",
                pricing_provider="cash-var-openai",
            )
            is True
        )
        # Live pricing (even without model_key or with model_key) returns subscription marker, NOT parent openai cash card
        assert find_model_pricing(
            "gpt-test", provider="cash-var-openai", model_key="team-sub-gpt"
        ) == {"pricing_type": "subscription"}
        assert find_model_pricing("gpt-test", provider="cash-var-openai") == {
            "pricing_type": "subscription"
        }

        snap = build_effective_catalog_snapshot(
            ["team-sub-gpt"],
            compiled=compiled,
            variant_credentials={
                "cash-var-openai": {
                    "bound_base_url": "https://cash-var.example.com/v1",
                    "canonical_bound_url": "https://cash-var.example.com:443/v1",
                    "key_fingerprint": "fp-cash-var",
                }
            },
        )
        assert snap is not None
        assert snap["models"]["team-sub-gpt"]["billing"] == "subscription"
        assert snap["models"]["team-sub-gpt"]["pricing"] is None

        record = {
            "model_name": "team-sub-gpt",
            "served_model": "gpt-test",
            "pricing_model_id": "gpt-test",
            "pricing_provider": "cash-var-openai",
            "billing_type": "platform",
            "usage": {
                "input_tokens": 100_000,
                "output_tokens": 50_000,
                "cached_tokens": 20_000,
            },
        }

        # Between-turns / live settlement (no snapshot bound) vs in-turn settlement (with snapshot bound)
        settled_live = calculate_cost_from_per_call_records([record])
        with bind_catalog_snapshot(snap):
            settled_snap = calculate_cost_from_per_call_records([record])

        assert settled_live["total_cost"] == 0.0
        assert settled_live["platform_cost"] == 0.0
        assert settled_snap["total_cost"] == 0.0
        assert settled_snap["platform_cost"] == 0.0
        assert settled_live["notional_cost"] > 0.0
        assert settled_live["notional_cost"] == pytest.approx(settled_snap["notional_cost"])
        assert settled_live.get("pricing_failed") is None
        assert settled_snap.get("pricing_failed") is None

    def test_r5_4_legacy_cash_card_on_subscription_model_allows_unrelated_edit_and_pricing_null_clear(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import copy

        from src.dalton.services.team_model_catalog import service as tmc_service
        from src.dalton.services.team_model_catalog.service import update_catalog_model
        from src.dalton.services.team_model_catalog.validation import CatalogModelUpsertInput

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        # Pre-existing legacy payload: subscription provider variant + model that already has a non-zero cash card
        db_state = {
            "revision": 5,
            "payload": {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "provider_variants": {
                        "openai": {
                            "sub-legacy-var": {
                                "parent_provider": "openai",
                                "display_name": "Legacy Sub Variant",
                                "sdk": "openai",
                                "billing": "subscription",
                                "base_url": "https://sub-legacy.example.com/v1",
                            }
                        }
                    },
                    "models": {
                        "sub-legacy-var": [
                            {
                                "id": "legacy-sub-id",
                                "name": "Legacy Sub",
                                "pricing": {"input": 5.0, "output": 15.0},
                            }
                        ]
                    },
                },
                "models": {
                    "legacy-sub-model": {
                        "model_id": "legacy-sub-id",
                        "provider": "sub-legacy-var",
                        "display_name": "Old Name",
                        "visible": True,
                    }
                },
                "dalton_meta": {
                    "legacy-sub-model": {
                        "billing": "subscription",
                        "tool_calling": True,
                        "notional_pricing": {"input": 5.0, "output": 15.0},
                    }
                },
            },
            "payload_sha256": "sha5",
            "legacy_sync_import_status": "not_applicable",
            "last_known_legacy_retired": [],
        }

        class _FakeConn:
            @asynccontextmanager
            async def transaction(self):
                yield

        @asynccontextmanager
        async def _fake_get_db(_conn=None):
            yield _FakeConn()

        async def _fake_get_row(conn: Any = None, *, for_update: bool = False) -> dict[str, Any]:
            return copy.deepcopy(db_state)

        async def _fake_save(
            conn: Any,
            *,
            expected_revision: int,
            action: str,
            target_keys: list[str],
            payload: dict[str, Any],
            diff_summary: dict[str, Any],
            side_effects: list[dict[str, Any]],
            actor_user_id: str,
            comment: str | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            db_state["revision"] = expected_revision + 1
            db_state["payload"] = copy.deepcopy(payload)
            return {
                "catalog": {"revision": db_state["revision"]},
                "revision_record": {"revision": db_state["revision"], "action": action},
            }

        monkeypatch.setattr(tmc_service, "get_db_connection", _fake_get_db)
        monkeypatch.setattr(
            tmc_service,
            "acquire_catalog_exclusive_lock",
            create_autospec(tmc_service.acquire_catalog_exclusive_lock),
        )
        monkeypatch.setattr(
            tmc_service,
            "get_catalog_row",
            create_autospec(tmc_service.get_catalog_row, side_effect=_fake_get_row),
        )
        monkeypatch.setattr(
            tmc_service,
            "save_catalog_revision",
            create_autospec(tmc_service.save_catalog_revision, side_effect=_fake_save),
        )
        monkeypatch.setattr(
            tmc_service,
            "publish_catalog_changed",
            create_autospec(tmc_service.publish_catalog_changed),
        )
        monkeypatch.setattr(
            tmc_service,
            "invalidate_catalog_redis_caches",
            create_autospec(tmc_service.invalidate_catalog_redis_caches),
        )

        # 1) Editing an unrelated field (display_name) succeeds even though a legacy cash card exists
        res1 = asyncio.run(
            update_catalog_model(
                actor_user_id="admin-1",
                model_key="legacy-sub-model",
                data=CatalogModelUpsertInput(display_name="Updated Name", expected_revision=5),
            )
        )
        assert res1["revision"] == 6
        assert db_state["payload"]["models"]["legacy-sub-model"]["display_name"] == "Updated Name"

        # 2) Explicitly setting new cash pricing rates is still rejected
        with pytest.raises(CatalogValidationError) as exc_info:
            asyncio.run(
                update_catalog_model(
                    actor_user_id="admin-1",
                    model_key="legacy-sub-model",
                    data=CatalogModelUpsertInput(
                        pricing={"input": 2.0, "output": 8.0},
                        expected_revision=6,
                    ),
                )
            )
        assert exc_info.value.code == "subscription_cash_pricing_forbidden"

        # 3) Explicitly passing pricing=None deletes the legacy cash card
        res2 = asyncio.run(
            update_catalog_model(
                actor_user_id="admin-1",
                model_key="legacy-sub-model",
                data=CatalogModelUpsertInput(pricing=None, expected_revision=6),
            )
        )
        assert res2["revision"] == 7
        assert "sub-legacy-var" not in (db_state["payload"].get("providers", {}).get("models") or {})

    def test_r5_5_disabling_native_oauth_sync_rejects_resuming_oauth_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.dalton.services.team_model_catalog.constants import (
            CATALOG_ROUTE_CHANGED_CODE,
            CATALOG_ROUTE_CHANGED_MESSAGE,
        )
        from src.dalton.services.team_model_catalog.snapshot import (
            build_effective_catalog_snapshot,
            resolve_live_route_fingerprint,
            verify_resume_route,
        )
        from src.llms.llm import LLM as LLMFactory

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        # Build snapshot while NATIVE_OAUTH_SYNC is enabled
        compiled_on = compile_catalog_from_payload(
            {"schema": CATALOG_SCHEMA}, revision=50, oauth_sync_enabled=True
        )
        snap = build_effective_catalog_snapshot(["grok-4.7-oauth"], compiled=compiled_on)
        assert "grok-4.7-oauth" in snap["models"]

        # Disable NATIVE_OAUTH_SYNC capability: live catalog drops grok-4.7-oauth
        compiled_off = compile_catalog_from_payload(
            {"schema": CATALOG_SCHEMA}, revision=51, oauth_sync_enabled=False
        )
        assert compiled_off.model_config.get_model_config("grok-4.7-oauth") is None
        cache_compiled_catalog(compiled_off)
        orig_get_compiled = compiled_mod.get_compiled_catalog_sync
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_compiled, return_value=compiled_off),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", compiled_off.model_config)

        # Live route is empty (model removed from catalog) -> verify_resume_route refuses with 400 catalog_route_changed
        interrupted_oauth_row = {
            "conversation_response_id": "33333333-3333-3333-3333-333333333333",
            "conversation_thread_id": "thread-r5-5",
            "metadata": {
                "llm_model": "grok-4.7-oauth",
                "effective_catalog_snapshot": snap,
            },
        }
        live_route = asyncio.run(resolve_live_route_fingerprint("grok-4.7-oauth"))
        assert live_route.provider == ""
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                verify_resume_route(
                    "33333333-3333-3333-3333-333333333333",
                    live_route,
                    thread_id="thread-r5-5",
                    run_row=interrupted_oauth_row,
                )
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == CATALOG_ROUTE_CHANGED_CODE
        assert exc_info.value.detail["message"] == CATALOG_ROUTE_CHANGED_MESSAGE

    def test_r5_7_model_list_tool_calling_null_does_not_backfill_stale_process_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.llm import role_policy

        # Warm process-level capability cache with rev 1 where gpt-test had tool_calling=True
        monkeypatch.setattr(
            role_policy,
            "_load_tool_capability_snapshot",
            create_autospec(
                role_policy._load_tool_capability_snapshot,
                return_value=(
                    {"gpt-test": {"tool_calling": True}},
                    {"smoke-fail-model": {"checks": {"N3": "fail"}}},
                    {"gpt-test": True, "smoke-fail-model": False},
                ),
            ),
        )

        # Rev 2 clears gpt-test tool_calling to null (key absent from dalton_meta["gpt-test"])
        rev2_meta = {
            "gpt-test": {"billing": "cash"},
            "explicit-false": {"billing": "cash", "tool_calling": False},
        }
        annotated = role_policy.annotate_model_metadata_tool_calling(
            {
                "gpt-test": {"provider": "openai"},
                "explicit-false": {"provider": "openai"},
                "smoke-fail-model": {"provider": "openai"},
            },
            dalton_meta=rev2_meta,
        )
        # Must NOT backfill stale True from process-cached resolved_cap
        assert "tool_calling" not in annotated["gpt-test"]
        assert annotated["explicit-false"]["tool_calling"] is False
        assert annotated["smoke-fail-model"]["tool_calling"] is False


# ---------------------------------------------------------------------------
# Batch 1 r6: mixed-route billing, subagent dict model, personal model shadow,
# shared pricing preservation, and dalton_complete unified billing regressions
# ---------------------------------------------------------------------------


class TestR6Fixes:
    """Regressions for Batch 1 r6 review findings."""

    def test_r6_1_mixed_cash_and_subscription_on_same_route_rejected_and_legacy_mixed_uses_own_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.billing import resolve_model_billing
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.dalton.services.team_model_catalog.snapshot import (
            bind_catalog_snapshot,
            build_effective_catalog_snapshot,
        )
        from src.dalton.services.team_model_catalog.validation import (
            trial_compile_and_validate_catalog,
        )
        from src.llms.llm import LLM as LLMFactory
        from src.utils.tracking.core import calculate_cost_from_per_call_records

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        # 1) Adding a subscription model on the same (openai, gpt-test) route as existing cash model gpt-test is rejected (400)
        mixed_payload = {
            "schema": CATALOG_SCHEMA,
            "models": {
                "gpt-test-sub": {
                    "model_id": "gpt-test",
                    "provider": "openai",
                    "display_name": "GPT Test Sub",
                    "visible": True,
                }
            },
            "dalton_meta": {
                "gpt-test-sub": {
                    "billing": "subscription",
                    "tool_calling": True,
                    "notional_pricing": {"input": 2.0, "output": 8.0, "cached_input": 0.2},
                }
            },
        }
        with pytest.raises(CatalogValidationError) as exc_info:
            trial_compile_and_validate_catalog(mixed_payload, revision=70)
        assert exc_info.value.status_code == 400
        assert exc_info.value.code == "mixed_route_billing_conflict"
        assert "gpt-test" in str(exc_info.value)
        assert "gpt-test-sub" in str(exc_info.value)

        # 2) Legacy mixed data already in catalog: each model settles strictly by its OWN key
        legacy_compiled = compile_catalog_from_payload(mixed_payload, revision=71)
        cache_compiled_catalog(legacy_compiled)
        orig_get_compiled = compiled_mod.get_compiled_catalog_sync
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_compiled, return_value=legacy_compiled),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", legacy_compiled.model_config)
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_dalton_meta",
            lambda *a, **kw: legacy_compiled.dalton_meta,
        )

        assert (
            resolve_model_billing(
                model_key="gpt-test",
                provider="openai",
                model_id="gpt-test",
                model_config=legacy_compiled.model_config,
                dalton_meta=legacy_compiled.dalton_meta,
            )
            == "cash"
        )
        assert (
            resolve_model_billing(
                model_key="gpt-test-sub",
                provider="openai",
                model_id="gpt-test",
                model_config=legacy_compiled.model_config,
                dalton_meta=legacy_compiled.dalton_meta,
            )
            == "subscription"
        )

        # Snapshot containing BOTH mixed keys must still settle gpt-test as cash and gpt-test-sub as subscription
        mixed_snap = build_effective_catalog_snapshot(
            ["gpt-test", "gpt-test-sub"], compiled=legacy_compiled
        )
        cash_record = {
            "model_name": "gpt-test",
            "served_model": "gpt-test",
            "pricing_model_id": "gpt-test",
            "pricing_provider": "openai",
            "billing_type": "platform",
            "usage": {"input_tokens": 1_000_000, "output_tokens": 100_000, "cached_tokens": 0},
        }
        sub_record = {
            "model_name": "gpt-test-sub",
            "served_model": "gpt-test",
            "pricing_model_id": "gpt-test",
            "pricing_provider": "openai",
            "billing_type": "platform",
            "usage": {"input_tokens": 1_000_000, "output_tokens": 100_000, "cached_tokens": 0},
        }

        cash_live = calculate_cost_from_per_call_records([cash_record])
        sub_live = calculate_cost_from_per_call_records([sub_record])
        with bind_catalog_snapshot(mixed_snap):
            cash_snap = calculate_cost_from_per_call_records([cash_record])
            sub_snap = calculate_cost_from_per_call_records([sub_record])

        assert cash_live["platform_cost"] > 0.0
        assert cash_live["notional_cost"] == 0.0
        assert cash_snap["platform_cost"] == pytest.approx(cash_live["platform_cost"])
        assert cash_snap["notional_cost"] == 0.0
        assert sub_live["platform_cost"] == 0.0
        assert sub_live["notional_cost"] > 0.0
        assert sub_snap["platform_cost"] == 0.0
        assert sub_snap["notional_cost"] == pytest.approx(sub_live["notional_cost"])

    def test_r6_2_subagent_dict_model_snapshot_extraction_and_verify_resume_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.dalton.services.team_model_catalog.constants import CATALOG_ROUTE_CHANGED_CODE
        from src.dalton.services.team_model_catalog.snapshot import (
            RouteFingerprint,
            _extract_candidate_models_from_run,
            _extract_model_key_value,
            _extract_primary_model_key_from_run,
            build_effective_catalog_snapshot,
            verify_resume_route,
        )
        from src.llms.llm import LLM as LLMFactory

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        payload = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "openai": {
                        "var-sub-a": {
                            "parent_provider": "openai",
                            "display_name": "Sub Var A",
                            "sdk": "openai",
                            "billing": "cash",
                            "base_url": "https://sub-a.example.com/v1",
                        }
                    }
                },
                "models": {
                    "var-sub-a": [
                        {"id": "sub-a-id", "name": "Sub A", "pricing": {"input": 1.0, "output": 2.0}}
                    ]
                },
            },
            "models": {
                "sub-model-a": {
                    "model_id": "sub-a-id",
                    "provider": "var-sub-a",
                    "display_name": "Sub Model A",
                    "visible": True,
                }
            },
            "dalton_meta": {"sub-model-a": {"billing": "cash", "tool_calling": True}},
        }
        compiled = compile_catalog_from_payload(payload, revision=80)
        cache_compiled_catalog(compiled)
        orig_get_compiled = compiled_mod.get_compiled_catalog_sync
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_compiled, return_value=compiled),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", compiled.model_config)

        # card_model produces dict {"id": "sub-model-a", "provider": "var-sub-a"} when Task() omits model
        dict_model = {"id": "sub-model-a", "provider": "var-sub-a"}
        assert _extract_model_key_value(dict_model) == "sub-model-a"

        # 1) First-write snapshot extraction handles SubagentModel dict (model_override=None)
        sub_models = [mk for raw_m in (None, dict_model) if (mk := _extract_model_key_value(raw_m))]
        assert sub_models == ["sub-model-a"]
        snap = build_effective_catalog_snapshot(
            sub_models,
            compiled=compiled,
            variant_credentials={
                "var-sub-a": {
                    "bound_base_url": "https://sub-a.example.com/v1",
                    "canonical_bound_url": "https://sub-a.example.com/v1",
                    "key_fingerprint": "fp-sub-a",
                }
            },
        )
        assert "sub-model-a" in snap["models"]
        assert snap["models"]["sub-model-a"]["key_fingerprint"] == "fp-sub-a"

        # 2) verify_resume_route extracts model_key from replay_facts.model.id:
        #    passes when unchanged, refuses when credential rotates.
        row_with_snap = {
            "task_run_id": "44444444-4444-4444-4444-444444444444",
            "thread_id": "thread-r6-2",
            "replay_facts": {
                "model": dict_model,
                "effective_catalog_snapshot": snap,
            },
        }
        assert _extract_candidate_models_from_run(row_with_snap) == ["sub-model-a"]
        assert (
            _extract_primary_model_key_from_run(row_with_snap, is_subagent=True, snapshot=snap)
            == "sub-model-a"
        )
        matching_route = RouteFingerprint(
            model_key="sub-model-a",
            provider="var-sub-a",
            model_id="sub-a-id",
            bound_base_url="https://sub-a.example.com/v1",
            key_fingerprint="fp-sub-a",
            billing="cash",
        )
        assert (
            asyncio.run(
                verify_resume_route(
                    "44444444-4444-4444-4444-444444444444",
                    matching_route,
                    is_subagent=True,
                    run_row=row_with_snap,
                )
            )
            is None
        )
        rotated_route = RouteFingerprint(
            model_key="sub-model-a",
            provider="var-sub-a",
            model_id="sub-a-id",
            bound_base_url="https://sub-a.example.com/v1",
            key_fingerprint="fp-sub-a-rotated",
            billing="cash",
        )
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                verify_resume_route(
                    "44444444-4444-4444-4444-444444444444",
                    rotated_route,
                    is_subagent=True,
                    run_row=row_with_snap,
                )
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == CATALOG_ROUTE_CHANGED_CODE

    def test_r6_3_resume_rejects_newly_added_same_name_personal_custom_model_shadow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.dalton.services.team_model_catalog.constants import (
            CATALOG_ROUTE_CHANGED_CODE,
            CATALOG_ROUTE_CHANGED_MESSAGE,
        )
        from src.dalton.services.team_model_catalog.snapshot import (
            build_effective_catalog_snapshot,
            resolve_live_route_fingerprint,
            verify_resume_route,
        )
        from src.llms.llm import LLM as LLMFactory

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        payload = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "openai": {
                        "team-var-x": {
                            "parent_provider": "openai",
                            "display_name": "Team Var X",
                            "sdk": "openai",
                            "billing": "cash",
                            "base_url": "https://team-x.example.com/v1",
                        }
                    }
                },
                "models": {
                    "team-var-x": [
                        {"id": "x-id", "name": "X", "pricing": {"input": 1.0, "output": 2.0}}
                    ]
                },
            },
            "models": {
                "team-model-x": {
                    "model_id": "x-id",
                    "provider": "team-var-x",
                    "display_name": "Team Model X",
                    "visible": True,
                }
            },
            "dalton_meta": {"team-model-x": {"billing": "cash", "tool_calling": True}},
        }
        compiled = compile_catalog_from_payload(payload, revision=90)
        cache_compiled_catalog(compiled)
        orig_get_compiled = compiled_mod.get_compiled_catalog_sync
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_compiled, return_value=compiled),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", compiled.model_config)

        snap = build_effective_catalog_snapshot(
            ["team-model-x"],
            compiled=compiled,
            variant_credentials={
                "team-var-x": {
                    "bound_base_url": "https://team-x.example.com/v1",
                    "canonical_bound_url": "https://team-x.example.com/v1",
                    "key_fingerprint": "fp-x",
                }
            },
        )
        row = {
            "conversation_response_id": "55555555-5555-5555-5555-555555555555",
            "conversation_thread_id": "thread-r6-3",
            "metadata": {
                "llm_model": "team-model-x",
                "effective_catalog_snapshot": snap,
            },
        }

        # User adds a same-name personal custom_models entry pointing at a personal OpenAI route
        pref_with_same_name_custom = {
            "custom_models": [
                {
                    "name": "team-model-x",
                    "model_id": "gpt-test",
                    "provider": "openai",
                    "display_name": "Personal Hijack",
                }
            ]
        }

        live_route = asyncio.run(
            resolve_live_route_fingerprint(
                "team-model-x",
                user_id="u-1",
                model_pref=pref_with_same_name_custom,
            )
        )
        assert live_route.provider == "openai"
        assert live_route.model_id == "gpt-test"
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                verify_resume_route(
                    "55555555-5555-5555-5555-555555555555",
                    live_route,
                    thread_id="thread-r6-3",
                    run_row=row,
                )
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == CATALOG_ROUTE_CHANGED_CODE
        assert exc_info.value.detail["message"] == CATALOG_ROUTE_CHANGED_MESSAGE

        # Conversely, if the original run itself was a personal custom model (no team snapshot entry),
        # resuming without change proceeds cleanly.
        personal_row = {
            "conversation_response_id": "55555555-5555-5555-5555-555555555556",
            "conversation_thread_id": "thread-r6-3",
            "metadata": {
                "llm_model": "team-model-x",
                "model_source": "custom",
            },
        }
        assert (
            asyncio.run(
                verify_resume_route(
                    "55555555-5555-5555-5555-555555555556",
                    live_route,
                    thread_id="thread-r6-3",
                    run_row=personal_row,
                )
            )
            is None
        )

    def test_r6_4_clearing_subscription_pricing_preserves_shared_pricing_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import copy

        from src.dalton.services.team_model_catalog import service as tmc_service
        from src.dalton.services.team_model_catalog.service import update_catalog_model
        from src.dalton.services.team_model_catalog.validation import CatalogModelUpsertInput

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        # Two subscription models sharing the same (sub-shared-var, shared-card-id) pricing card & override
        db_state = {
            "revision": 10,
            "payload": {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "provider_variants": {
                        "openai": {
                            "sub-shared-var": {
                                "parent_provider": "openai",
                                "display_name": "Shared Sub Var",
                                "sdk": "openai",
                                "billing": "subscription",
                                "base_url": "https://sub-shared.example.com/v1",
                            }
                        }
                    },
                    "models": {
                        "sub-shared-var": [
                            {
                                "id": "shared-card-id",
                                "name": "Shared Card",
                                "pricing": {"input": 5.0, "output": 15.0},
                            }
                        ]
                    },
                    "model_overrides": {
                        "sub-shared-var": {
                            "shared-card-id": {"pricing": {"input": 6.0, "output": 18.0}}
                        }
                    },
                },
                "models": {
                    "sub-model-1": {
                        "model_id": "shared-card-id",
                        "provider": "sub-shared-var",
                        "display_name": "Sub Model 1",
                        "visible": True,
                    },
                    "sub-model-2": {
                        "model_id": "shared-card-id",
                        "provider": "sub-shared-var",
                        "display_name": "Sub Model 2",
                        "visible": True,
                    },
                },
                "dalton_meta": {
                    "sub-model-1": {
                        "billing": "subscription",
                        "tool_calling": True,
                        "notional_pricing": {"input": 5.0, "output": 15.0},
                    },
                    "sub-model-2": {
                        "billing": "subscription",
                        "tool_calling": True,
                        "notional_pricing": {"input": 5.0, "output": 15.0},
                    },
                },
            },
            "payload_sha256": "sha10",
            "legacy_sync_import_status": "not_applicable",
            "last_known_legacy_retired": [],
        }

        class _FakeConn:
            @asynccontextmanager
            async def transaction(self):
                yield

        @asynccontextmanager
        async def _fake_get_db(_conn=None):
            yield _FakeConn()

        async def _fake_get_row(conn: Any = None, *, for_update: bool = False) -> dict[str, Any]:
            return copy.deepcopy(db_state)

        async def _fake_save(
            conn: Any,
            *,
            expected_revision: int,
            action: str,
            target_keys: list[str],
            payload: dict[str, Any],
            diff_summary: dict[str, Any],
            side_effects: list[dict[str, Any]],
            actor_user_id: str,
            comment: str | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            db_state["revision"] = expected_revision + 1
            db_state["payload"] = copy.deepcopy(payload)
            return {
                "catalog": {"revision": db_state["revision"]},
                "revision_record": {"revision": db_state["revision"], "action": action},
            }

        monkeypatch.setattr(tmc_service, "get_db_connection", _fake_get_db)
        monkeypatch.setattr(
            tmc_service,
            "acquire_catalog_exclusive_lock",
            create_autospec(tmc_service.acquire_catalog_exclusive_lock),
        )
        monkeypatch.setattr(
            tmc_service,
            "get_catalog_row",
            create_autospec(tmc_service.get_catalog_row, side_effect=_fake_get_row),
        )
        monkeypatch.setattr(
            tmc_service,
            "save_catalog_revision",
            create_autospec(tmc_service.save_catalog_revision, side_effect=_fake_save),
        )
        monkeypatch.setattr(
            tmc_service,
            "publish_catalog_changed",
            create_autospec(tmc_service.publish_catalog_changed),
        )
        monkeypatch.setattr(
            tmc_service,
            "invalidate_catalog_redis_caches",
            create_autospec(tmc_service.invalidate_catalog_redis_caches),
        )

        # Clearing pricing=None on sub-model-1 must NOT delete the shared pricing card or its override
        res = asyncio.run(
            update_catalog_model(
                actor_user_id="admin-1",
                model_key="sub-model-1",
                data=CatalogModelUpsertInput(pricing=None, expected_revision=10),
            )
        )
        assert res["revision"] == 11
        saved_provs = db_state["payload"]["providers"]
        assert len(saved_provs["models"]["sub-shared-var"]) == 1
        assert saved_provs["model_overrides"]["sub-shared-var"]["shared-card-id"] == {
            "pricing": {"input": 6.0, "output": 18.0}
        }

    def test_r6_5_dalton_complete_provider_level_subscription_skips_cash_budget_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.llm import complete as complete_mod
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.llms.llm import LLM as LLMFactory
        from src.server.services.llm import user_models

        orig_get_pref = user_models.get_model_preference
        monkeypatch.setattr(
            user_models,
            "get_model_preference",
            create_autospec(orig_get_pref, return_value={}),
        )

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        # Provider-level billing="subscription", model_key has NO -oauth suffix and NO dalton_meta.billing
        payload = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "openai": {
                        "corp-sub-var": {
                            "parent_provider": "openai",
                            "display_name": "Corp Sub Variant",
                            "sdk": "openai",
                            "billing": "subscription",
                            "base_url": "https://corp-sub.example.com/v1",
                        }
                    }
                }
            },
            "models": {
                "corp-sub-plain-key": {
                    "model_id": "corp-sub-1",
                    "provider": "corp-sub-var",
                    "display_name": "Corp Sub Plain",
                    "visible": True,
                }
            },
            "dalton_meta": {
                "corp-sub-plain-key": {
                    "tool_calling": True,
                    "notional_pricing": {"input": 2.0, "output": 8.0},
                }
            },
        }
        compiled = compile_catalog_from_payload(payload, revision=95)
        cache_compiled_catalog(compiled)
        orig_get_compiled = compiled_mod.get_compiled_catalog_sync
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_compiled, return_value=compiled),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", compiled.model_config)
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_dalton_meta",
            lambda *a, **kw: compiled.dalton_meta,
        )

        b_type, prov = complete_mod.resolve_model_billing_and_provider("corp-sub-plain-key")
        assert b_type == "oauth"
        assert prov == "corp-sub-var"

        # Even if cash budget is exhausted, dalton_complete skips _check_cash_budget_admission
        orig_budget_check = complete_mod._check_cash_budget_admission
        budget_mock = create_autospec(
            orig_budget_check,
            return_value=(False, "budget_exhausted", "今日现金额度已用完"),
        )
        monkeypatch.setattr(complete_mod, "_check_cash_budget_admission", budget_mock)
        orig_inspect = complete_mod.inspect_journal_state
        monkeypatch.setattr(
            complete_mod,
            "inspect_journal_state",
            create_autospec(orig_inspect, return_value=("proceed", None)),
        )
        orig_claim = complete_mod.claim_journal_invocation
        monkeypatch.setattr(
            complete_mod,
            "claim_journal_invocation",
            create_autospec(orig_claim, return_value=("in_flight", {"attempt": 1})),
        )

        res = asyncio.run(
            complete_mod.dalton_complete(
                user_id="u-1",
                purpose="test_sub_budget",
                user_prompt="ping",
                model="corp-sub-plain-key",
            )
        )
        assert budget_mock.call_count == 0
        assert res.status != "budget_exhausted"


# ---------------------------------------------------------------------------
# Batch 1 r7: compare-and-reject resume guard (verify_resume_route) +
# R6 billing-only fixes (-oauth suffix explicit cash & alias pricing: null)
# ---------------------------------------------------------------------------


class TestR7Fixes:
    """Regressions for Batch 1 r7 compare-and-reject resume guard and R6 billing fixes."""

    def test_r7_1_verify_resume_route_allows_unchanged_and_legacy_and_rejects_each_field_change(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.team_model_catalog.constants import (
            CATALOG_ROUTE_CHANGED_CODE,
            CATALOG_ROUTE_CHANGED_MESSAGE,
        )
        from src.dalton.services.team_model_catalog.snapshot import (
            RouteFingerprint,
            build_effective_catalog_snapshot,
            verify_resume_route,
        )

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        cat = compile_catalog_from_payload(
            {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "provider_variants": {
                        "openai": {
                            "var-r7": {
                                "parent_provider": "openai",
                                "display_name": "Var R7",
                                "sdk": "openai",
                                "billing": "cash",
                                "base_url": "https://r7.example.com/v1",
                            }
                        }
                    },
                    "models": {
                        "var-r7": [
                            {"id": "r7-id", "name": "R7", "pricing": {"input": 1.0, "output": 2.0}}
                        ]
                    },
                },
                "models": {
                    "r7-model": {
                        "model_id": "r7-id",
                        "provider": "var-r7",
                        "display_name": "R7 Model",
                        "visible": True,
                    }
                },
                "dalton_meta": {"r7-model": {"billing": "cash", "tool_calling": True}},
            },
            revision=101,
        )
        snap = build_effective_catalog_snapshot(
            ["r7-model"],
            compiled=cat,
            variant_credentials={
                "var-r7": {
                    "bound_base_url": "https://r7.example.com/v1",
                    "canonical_bound_url": "https://r7.example.com:443/v1",
                    "key_fingerprint": "fp-r7-1",
                }
            },
        )
        run_row = {
            "conversation_response_id": "77777777-7777-4777-8777-777777777771",
            "conversation_thread_id": "thread-r7-1",
            "metadata": {"llm_model": "r7-model", "effective_catalog_snapshot": snap},
        }
        baseline = RouteFingerprint(
            model_key="r7-model",
            provider="var-r7",
            model_id="r7-id",
            bound_base_url="https://r7.example.com/v1",
            key_fingerprint="fp-r7-1",
            billing="cash",
        )
        # 1) Unchanged route proceeds cleanly
        assert (
            asyncio.run(
                verify_resume_route(
                    "77777777-7777-4777-8777-777777777771", baseline, run_row=run_row
                )
            )
            is None
        )

        # 2) Every one of the 6 route fingerprint fields triggers 400 catalog_route_changed
        mutations = [
            ("model_key", RouteFingerprint(model_key="other-model", provider="var-r7", model_id="r7-id", bound_base_url="https://r7.example.com/v1", key_fingerprint="fp-r7-1", billing="cash")),
            ("provider", RouteFingerprint(model_key="r7-model", provider="var-r7-new", model_id="r7-id", bound_base_url="https://r7.example.com/v1", key_fingerprint="fp-r7-1", billing="cash")),
            ("model_id", RouteFingerprint(model_key="r7-model", provider="var-r7", model_id="r7-id-v2", bound_base_url="https://r7.example.com/v1", key_fingerprint="fp-r7-1", billing="cash")),
            ("bound_base_url", RouteFingerprint(model_key="r7-model", provider="var-r7", model_id="r7-id", bound_base_url="https://other.example.com/v1", key_fingerprint="fp-r7-1", billing="cash")),
            ("key_fingerprint", RouteFingerprint(model_key="r7-model", provider="var-r7", model_id="r7-id", bound_base_url="https://r7.example.com/v1", key_fingerprint="fp-r7-rotated", billing="cash")),
            ("billing", RouteFingerprint(model_key="r7-model", provider="var-r7", model_id="r7-id", bound_base_url="https://r7.example.com/v1", key_fingerprint="fp-r7-1", billing="subscription")),
        ]
        for field_name, mutated_route in mutations:
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(
                    verify_resume_route(
                        "77777777-7777-4777-8777-777777777771",
                        mutated_route,
                        run_row=run_row,
                    )
                )
            assert exc_info.value.status_code == 400
            detail = exc_info.value.detail
            assert isinstance(detail, dict)
            assert detail["type"] == CATALOG_ROUTE_CHANGED_CODE
            assert detail["code"] == CATALOG_ROUTE_CHANGED_CODE
            assert detail["message"] == CATALOG_ROUTE_CHANGED_MESSAGE
            assert field_name in detail["diffs"]

        # 3) Legacy run with no persisted effective_catalog_snapshot continues unchanged
        legacy_row = {
            "conversation_response_id": "77777777-7777-4777-8777-777777777772",
            "conversation_thread_id": "thread-r7-1",
            "metadata": {"llm_model": "r7-model"},
        }
        assert (
            asyncio.run(
                verify_resume_route(
                    "77777777-7777-4777-8777-777777777772",
                    mutations[1][1],
                    run_row=legacy_row,
                )
            )
            is None
        )

    def test_r7_2_main_turn_resolve_llm_config_verifies_resume_route_before_client_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import copy
        from types import SimpleNamespace

        from ptc_agent.config import LLMConfig
        from src.dalton.database import team_model_catalog as tmc_db
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.dalton.services.team_model_catalog.constants import (
            CATALOG_ROUTE_CHANGED_CODE,
            CATALOG_ROUTE_CHANGED_MESSAGE,
        )
        from src.dalton.services.team_model_catalog.snapshot import (
            build_effective_catalog_snapshot,
            resume_verify_scope,
        )
        from src.llms.llm import LLM as LLMFactory
        from src.server.services.account import Policy
        from src.server.services.llm import config as llm_config_mod
        from src.server.services.llm import user_models

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        payload_v1 = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "openai": {
                        "gw-a": {
                            "parent_provider": "openai",
                            "display_name": "GW A",
                            "sdk": "openai",
                            "billing": "cash",
                            "base_url": "https://gw-a.example.com/v1",
                        },
                        "gw-b": {
                            "parent_provider": "openai",
                            "display_name": "GW B",
                            "sdk": "openai",
                            "billing": "cash",
                            "base_url": "https://gw-b.example.com/v1",
                        },
                    }
                },
                "models": {
                    "gw-a": [{"id": "m-1", "name": "M1", "pricing": {"input": 1.0, "output": 2.0}}],
                    "gw-b": [{"id": "m-1", "name": "M1", "pricing": {"input": 1.0, "output": 2.0}}],
                },
            },
            "models": {
                "team-r7": {
                    "model_id": "m-1",
                    "provider": "gw-a",
                    "display_name": "Team R7",
                    "visible": True,
                }
            },
            "dalton_meta": {"team-r7": {"billing": "cash", "tool_calling": True}},
        }
        cat_v1 = compile_catalog_from_payload(payload_v1, revision=110)
        snap_v1 = build_effective_catalog_snapshot(
            ["team-r7"],
            compiled=cat_v1,
            variant_credentials={
                "gw-a": {
                    "bound_base_url": "https://gw-a.example.com/v1",
                    "canonical_bound_url": "https://gw-a.example.com:443/v1",
                    "key_fingerprint": "fp-gw-a",
                }
            },
        )
        interrupted_row = {
            "conversation_response_id": "88888888-8888-4888-8888-888888888881",
            "conversation_thread_id": "thread-r7-2",
            "metadata": {"llm_model": "team-r7", "effective_catalog_snapshot": snap_v1},
        }
        legacy_row = {
            "conversation_response_id": "88888888-8888-4888-8888-888888888882",
            "conversation_thread_id": "thread-r7-2",
            "metadata": {"llm_model": "team-r7"},
        }

        class _FakeAgentConfig:
            def __init__(self) -> None:
                self.llm = LLMConfig(name="team-r7")
                self.llm_client = None
                self.claude_code = False
                self.claude_code_admitted = None
                self.claude_code_pins = None
                self.prompt_guidance = ""
                self.subagents = SimpleNamespace(resolved=None)
                self.search_api = "tavily"
                self.search_depth = "basic"

            def model_copy(self, *, deep: bool = False):
                return copy.deepcopy(self)

        monkeypatch.setattr(
            llm_config_mod,
            "require_policy",
            create_autospec(llm_config_mod.require_policy, return_value=Policy()),
        )
        monkeypatch.setattr(
            user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value={}),
        )
        monkeypatch.setattr(
            llm_config_mod,
            "gate_models_by_plan",
            create_autospec(
                llm_config_mod.gate_models_by_plan,
                side_effect=lambda _bc, _uid, mp, _rm, **kw: mp,
            ),
        )
        monkeypatch.setattr(
            llm_config_mod,
            "load_user_context",
            create_autospec(llm_config_mod.load_user_context),
        )
        monkeypatch.setattr(
            llm_config_mod,
            "default_to_first_allowed",
            create_autospec(llm_config_mod.default_to_first_allowed),
        )
        monkeypatch.setattr(
            llm_config_mod,
            "apply_compaction",
            create_autospec(llm_config_mod.apply_compaction),
        )
        monkeypatch.setattr(
            llm_config_mod,
            "gate_capabilities",
            create_autospec(llm_config_mod.gate_capabilities),
        )
        monkeypatch.setattr(
            llm_config_mod,
            "check_model_available",
            create_autospec(llm_config_mod.check_model_available, return_value=False),
        )
        monkeypatch.setattr(
            llm_config_mod,
            "subagent_roles",
            create_autospec(llm_config_mod.subagent_roles, return_value=[]),
        )
        monkeypatch.setattr(
            llm_config_mod,
            "turn_roster",
            create_autospec(llm_config_mod.turn_roster, return_value=SimpleNamespace()),
        )
        monkeypatch.setattr(
            llm_config_mod,
            "demote_clientless",
            create_autospec(llm_config_mod.demote_clientless),
        )
        monkeypatch.setattr(
            llm_config_mod,
            "subagent_models",
            create_autospec(llm_config_mod.subagent_models, return_value={}),
        )
        resolve_clients_spy = create_autospec(llm_config_mod.resolve_clients)
        monkeypatch.setattr(llm_config_mod, "resolve_clients", resolve_clients_spy)

        cred_state: dict[str, dict[str, Any]] = {
            "gw-a": {
                "provider_slug": "gw-a",
                "bound_base_url": "https://gw-a.example.com/v1",
                "canonical_bound_url": "https://gw-a.example.com:443/v1",
                "key_fingerprint": "fp-gw-a",
            },
            "gw-b": {
                "provider_slug": "gw-b",
                "bound_base_url": "https://gw-b.example.com/v1",
                "canonical_bound_url": "https://gw-b.example.com:443/v1",
                "key_fingerprint": "fp-gw-b",
            },
        }
        monkeypatch.setattr(
            tmc_db,
            "get_catalog_provider_credential",
            create_autospec(
                tmc_db.get_catalog_provider_credential,
                side_effect=lambda slug, conn=None, decrypt=True: cred_state.get(slug),
            ),
        )

        # Case A: unchanged live catalog -> verify_resume_route passes and resolve_clients IS called
        cache_compiled_catalog(cat_v1)
        orig_get_compiled = compiled_mod.get_compiled_catalog_sync
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_compiled, return_value=cat_v1),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", cat_v1.model_config)
        with resume_verify_scope(
            "88888888-8888-4888-8888-888888888881",
            thread_id="thread-r7-2",
            run_row=interrupted_row,
        ):
            asyncio.run(
                llm_config_mod.resolve_llm_config(
                    _FakeAgentConfig(),
                    "u-1",
                    request_model="team-r7",
                    is_byok=False,
                    thread_id="thread-r7-2",
                    user_facing=False,
                )
            )
        assert resolve_clients_spy.call_count == 1

        # Case B: provider changed gw-a -> gw-b -> 400 catalog_route_changed BEFORE resolve_clients
        resolve_clients_spy.reset_mock()
        payload_v2 = copy.deepcopy(payload_v1)
        payload_v2["models"]["team-r7"]["provider"] = "gw-b"
        cat_v2 = compile_catalog_from_payload(payload_v2, revision=111)
        cache_compiled_catalog(cat_v2)
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_compiled, return_value=cat_v2),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", cat_v2.model_config)
        with resume_verify_scope(
            "88888888-8888-4888-8888-888888888881",
            thread_id="thread-r7-2",
            run_row=interrupted_row,
        ):
            with pytest.raises(HTTPException) as exc_prov:
                asyncio.run(
                    llm_config_mod.resolve_llm_config(
                        _FakeAgentConfig(),
                        "u-1",
                        request_model="team-r7",
                        is_byok=False,
                        thread_id="thread-r7-2",
                        user_facing=False,
                    )
                )
        assert exc_prov.value.status_code == 400
        assert exc_prov.value.detail["code"] == CATALOG_ROUTE_CHANGED_CODE
        assert exc_prov.value.detail["message"] == CATALOG_ROUTE_CHANGED_MESSAGE
        assert resolve_clients_spy.call_count == 0

        # Case C: legacy run without snapshot proceeds even after catalog change
        with resume_verify_scope(
            "88888888-8888-4888-8888-888888888882",
            thread_id="thread-r7-2",
            run_row=legacy_row,
        ):
            asyncio.run(
                llm_config_mod.resolve_llm_config(
                    _FakeAgentConfig(),
                    "u-1",
                    request_model="team-r7",
                    is_byok=False,
                    thread_id="thread-r7-2",
                    user_facing=False,
                )
            )
        assert resolve_clients_spy.call_count == 1

    def test_r7_3_all_resume_entry_points_bind_resume_verify_scope(self) -> None:
        import inspect

        from src.server.app.threads import messaging
        from src.server.handlers.chat import ptc_run
        from src.server.services import automation_executor
        from src.server.services.llm import config as llm_config_mod

        msg_src = inspect.getsource(messaging._handle_send_message)
        assert "resume_verify_scope" in msg_src
        assert "retry_of_run_id" in msg_src
        assert "resume_of_run_id" in msg_src
        assert "request.hitl_response" in msg_src
        assert msg_src.index("resume_verify_scope") < msg_src.index(
            "await resolve_turn_config_with_automation_inheritance("
        )

        retry_src = inspect.getsource(messaging.retry_thread)
        assert "resume_source_run=latest" in retry_src

        cfg_src = inspect.getsource(llm_config_mod.resolve_llm_config)
        assert "await verify_resume_route(" in cfg_src
        assert cfg_src.index("await verify_resume_route(") < cfg_src.index("await resolve_clients(")

        ptc_src = inspect.getsource(ptc_run.astream_ptc_workflow)
        assert "resume_verify_scope" in ptc_src

        auto_src = inspect.getsource(automation_executor.AutomationExecutor._run_turn)
        assert "resume_of_run_id" in auto_src

    def test_r7_4_subagent_resume_allows_unchanged_and_legacy_without_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from langchain_core.messages import ToolMessage

        from ptc_agent.agent.middleware.background_subagent import task_actions
        from ptc_agent.config.agent import TaskAsk, TaskModelRefused
        from src.dalton.database import team_model_catalog as tmc_db
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.dalton.services.team_model_catalog.snapshot import (
            build_effective_catalog_snapshot,
        )
        from src.llms.llm import LLM as LLMFactory

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        payload = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "openai": {
                        "sub-gw": {
                            "parent_provider": "openai",
                            "display_name": "Sub GW",
                            "sdk": "openai",
                            "billing": "cash",
                            "base_url": "https://sub-gw.example.com/v1",
                        }
                    }
                },
                "models": {
                    "sub-gw": [
                        {"id": "sub-1", "name": "Sub 1", "pricing": {"input": 1.0, "output": 2.0}}
                    ]
                },
            },
            "models": {
                "sub-ok": {
                    "model_id": "sub-1",
                    "provider": "sub-gw",
                    "display_name": "Sub OK",
                    "visible": True,
                }
            },
            "dalton_meta": {"sub-ok": {"billing": "cash", "tool_calling": True}},
        }
        cat = compile_catalog_from_payload(payload, revision=120)
        cache_compiled_catalog(cat)
        orig_get_compiled = compiled_mod.get_compiled_catalog_sync
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_compiled, return_value=cat),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", cat.model_config)
        monkeypatch.setattr(
            tmc_db,
            "get_catalog_provider_credential",
            create_autospec(
                tmc_db.get_catalog_provider_credential,
                return_value={
                    "provider_slug": "sub-gw",
                    "bound_base_url": "https://sub-gw.example.com/v1",
                    "canonical_bound_url": "https://sub-gw.example.com:443/v1",
                    "key_fingerprint": "fp-sub-ok",
                },
            ),
        )
        snap = build_effective_catalog_snapshot(
            ["sub-ok"],
            compiled=cat,
            variant_credentials={
                "sub-gw": {
                    "bound_base_url": "https://sub-gw.example.com/v1",
                    "canonical_bound_url": "https://sub-gw.example.com:443/v1",
                    "key_fingerprint": "fp-sub-ok",
                }
            },
        )

        # Sentinel TaskModelRefused stops _handle_resume right after _resolve_model is invoked
        sentinel_refusal = TaskModelRefused(
            message="sentinel-reached-resolve-model", reason="sentinel"
        )
        resolve_spy = create_autospec(task_actions._resolve_model, return_value=sentinel_refusal)
        monkeypatch.setattr(task_actions, "_resolve_model", resolve_spy)

        fake_task = SimpleNamespace(
            task_id="sub-ok-1",
            display_id="Task-sub-ok-1",
            subagent_type="research",
            is_pending=False,
            cancelled=False,
            terminal_status=None,
            asyncio_task=None,
            result=None,
            ask=TaskAsk(model="sub-ok"),
            lock=asyncio.Lock(),
        )

        async def _resolve_ok_task(*_a: Any, **_kw: Any) -> Any:
            return fake_task

        fake_mw = SimpleNamespace(
            _resolve_or_error=_resolve_ok_task,
            subagent_types={"research"},
            _resume_claims=set(),
            registry=SimpleNamespace(
                get_by_id=lambda tid: fake_task if tid == "sub-ok-1" else None,
                run_ledger=object(),
            ),
            task_models=SimpleNamespace(
                _own_model=lambda _st: "sub-ok",
                _user_id="u-1",
                _model_pref={},
            ),
            namespace_owner=None,
        )
        fake_request = SimpleNamespace(
            tool_call={"id": "tc-ok-1", "args": {"action": "resume", "task_id": "sub-ok-1"}},
            state={},
        )

        # 1) Unchanged route passes verify_resume_route and reaches _resolve_model
        row_ok = {
            "task_run_id": "99999999-9999-4999-8999-999999999991",
            "thread_id": "thread-r7-4",
            "status": "completed",
            "replay_facts": {
                "model_override": "sub-ok",
                "effective_catalog_snapshot": snap,
            },
        }
        orig_read_latest = task_actions._read_latest_run
        monkeypatch.setattr(
            task_actions,
            "_read_latest_run",
            create_autospec(orig_read_latest, return_value=row_ok),
        )
        res1 = asyncio.run(
            task_actions._handle_resume(
                fake_mw,
                fake_request,
                handler=lambda _req: None,
                target_task_id="sub-ok-1",
                subagent_type=None,
                description="resume sub-ok-1",
                prompt="continue",
                args={"action": "resume", "task_id": "sub-ok-1"},
                parent_thread_id="thread-r7-4",
                tool_call_id="tc-ok-1",
                current_run_id="parent-run-ok-1",
                ask=TaskAsk(),
            )
        )
        assert isinstance(res1, ToolMessage)
        assert resolve_spy.call_count == 1
        assert "sentinel" in str(res1.content)

        # 2) Legacy run without snapshot passes verify_resume_route and reaches _resolve_model
        resolve_spy.reset_mock()
        row_legacy = {
            "task_run_id": "99999999-9999-4999-8999-999999999992",
            "thread_id": "thread-r7-4",
            "status": "completed",
            "replay_facts": {"model_override": "sub-ok"},
        }
        monkeypatch.setattr(
            task_actions,
            "_read_latest_run",
            create_autospec(orig_read_latest, return_value=row_legacy),
        )
        res2 = asyncio.run(
            task_actions._handle_resume(
                fake_mw,
                fake_request,
                handler=lambda _req: None,
                target_task_id="sub-ok-1",
                subagent_type=None,
                description="resume sub-ok-1 legacy",
                prompt="continue",
                args={"action": "resume", "task_id": "sub-ok-1"},
                parent_thread_id="thread-r7-4",
                tool_call_id="tc-ok-2",
                current_run_id="parent-run-ok-2",
                ask=TaskAsk(),
            )
        )
        assert isinstance(res2, ToolMessage)
        assert resolve_spy.call_count == 1
        assert "sentinel" in str(res2.content)

    def test_r7_5_oauth_suffix_with_explicit_cash_billing_enforces_cash_budget_on_route_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.llm import complete as complete_mod
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.billing import resolve_model_billing
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.llms.llm import LLM as LLMFactory
        from src.server.services.llm import user_models

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        # Sole catalog entry on (openai, m-cash-route) has -oauth suffix AND explicit dalton_meta.billing="cash"
        payload = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "models": {
                    "openai": [
                        {
                            "id": "m-cash-route",
                            "name": "Cash Route",
                            "pricing": {"input": 2.0, "output": 8.0},
                        }
                    ]
                }
            },
            "models": {
                "team-cash-oauth": {
                    "model_id": "m-cash-route",
                    "provider": "openai",
                    "display_name": "Team Cash OAuth Named",
                    "visible": True,
                }
            },
            "dalton_meta": {
                "team-cash-oauth": {
                    "billing": "cash",
                    "tool_calling": True,
                }
            },
        }
        cat = compile_catalog_from_payload(payload, revision=130, oauth_sync_enabled=True)
        cache_compiled_catalog(cat)
        orig_get_compiled = compiled_mod.get_compiled_catalog_sync
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_compiled, return_value=cat),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", cat.model_config)
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_dalton_meta",
            lambda *a, **kw: cat.dalton_meta,
        )

        # Route-level lookup from a differently-named personal custom model must resolve to "cash", NOT "subscription"
        assert (
            resolve_model_billing(
                model_key="my-personal-alias",
                provider="openai",
                model_id="m-cash-route",
                model_config=cat.model_config,
                dalton_meta=cat.dalton_meta,
            )
            == "cash"
        )

        # dalton_complete with personal custom model on (openai, m-cash-route) enforces cash budget admission!
        pref_with_custom = {
            "custom_models": [
                {
                    "name": "my-personal-alias",
                    "provider": "openai",
                    "model_id": "m-cash-route",
                    "display_name": "My Personal Alias",
                }
            ]
        }
        monkeypatch.setattr(
            user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=pref_with_custom),
        )
        budget_spy = create_autospec(
            complete_mod._check_cash_budget_admission,
            return_value=(False, "budget_exhausted", "今日现金额度已用完"),
        )
        monkeypatch.setattr(complete_mod, "_check_cash_budget_admission", budget_spy)
        monkeypatch.setattr(
            complete_mod,
            "inspect_journal_state",
            create_autospec(complete_mod.inspect_journal_state, return_value=("proceed", None)),
        )
        monkeypatch.setattr(
            complete_mod,
            "claim_journal_invocation",
            create_autospec(
                complete_mod.claim_journal_invocation,
                return_value=("in_flight", {"attempt": 1}),
            ),
        )

        res = asyncio.run(
            complete_mod.dalton_complete(
                user_id="u-1",
                purpose="test_cash_oauth_suffix_budget",
                user_prompt="ping",
                model="my-personal-alias",
            )
        )
        assert budget_spy.call_count == 1
        assert res.ok is False
        assert res.status == "budget_exhausted"

    def test_r7_6_alias_referenced_cash_card_cleared_on_pricing_null_when_unshared_and_kept_when_shared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import copy

        from src.dalton.services.team_model_catalog import service as tmc_service
        from src.dalton.services.team_model_catalog.service import update_catalog_model
        from src.dalton.services.team_model_catalog.validation import CatalogModelUpsertInput

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        def _make_state(*, with_second_model: bool) -> dict[str, Any]:
            models_dict: dict[str, Any] = {
                "alias-model-1": {
                    "model_id": "card-canon",
                    "provider": "var-alias",
                    "display_name": "Alias Model 1",
                    "visible": True,
                }
            }
            meta_dict: dict[str, Any] = {
                "alias-model-1": {"billing": "cash", "tool_calling": True}
            }
            if with_second_model:
                models_dict["alias-model-2"] = {
                    "model_id": "card-canon",
                    "provider": "var-alias",
                    "display_name": "Alias Model 2",
                    "visible": True,
                }
                meta_dict["alias-model-2"] = {
                    "billing": "subscription",
                    "tool_calling": True,
                    "notional_pricing": {"input": 3.0, "output": 9.0},
                }
            return {
                "revision": 15,
                "payload": {
                    "schema": CATALOG_SCHEMA,
                    "providers": {
                        "provider_variants": {
                            "openai": {
                                "var-alias": {
                                    "parent_provider": "openai",
                                    "display_name": "Var Alias",
                                    "sdk": "openai",
                                    "billing": "cash",
                                    "base_url": "https://var-alias.example.com/v1",
                                }
                            }
                        },
                        "models": {
                            "var-alias": [
                                {
                                    "id": "card-canon",
                                    "alias": ["card-alias"],
                                    "name": "Canon Card",
                                    "pricing": {"input": 3.0, "output": 9.0},
                                }
                            ]
                        },
                    },
                    "models": models_dict,
                    "dalton_meta": meta_dict,
                },
                "payload_sha256": "sha15",
                "legacy_sync_import_status": "not_applicable",
                "last_known_legacy_retired": [],
            }

        class _FakeConn:
            @asynccontextmanager
            async def transaction(self):
                yield

        @asynccontextmanager
        async def _fake_get_db(_conn=None):
            yield _FakeConn()

        orig_get_row = tmc_service.get_catalog_row
        orig_save = tmc_service.save_catalog_revision
        monkeypatch.setattr(tmc_service, "get_db_connection", _fake_get_db)
        monkeypatch.setattr(
            tmc_service,
            "acquire_catalog_exclusive_lock",
            create_autospec(tmc_service.acquire_catalog_exclusive_lock),
        )
        monkeypatch.setattr(
            tmc_service,
            "publish_catalog_changed",
            create_autospec(tmc_service.publish_catalog_changed),
        )
        monkeypatch.setattr(
            tmc_service,
            "invalidate_catalog_redis_caches",
            create_autospec(tmc_service.invalidate_catalog_redis_caches),
        )

        # Case A: sole user changes model_id to alias "card-alias", switches to subscription + pricing=None
        state_a = _make_state(with_second_model=False)

        async def _get_row_a(conn: Any = None, *, for_update: bool = False) -> dict[str, Any]:
            return copy.deepcopy(state_a)

        async def _save_a(
            conn: Any,
            *,
            expected_revision: int,
            action: str,
            target_keys: list[str],
            payload: dict[str, Any],
            diff_summary: dict[str, Any],
            side_effects: list[dict[str, Any]],
            actor_user_id: str,
            comment: str | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            state_a["revision"] = expected_revision + 1
            state_a["payload"] = copy.deepcopy(payload)
            return {
                "catalog": {"revision": state_a["revision"]},
                "revision_record": {"revision": state_a["revision"], "action": action},
            }

        monkeypatch.setattr(
            tmc_service, "get_catalog_row", create_autospec(orig_get_row, side_effect=_get_row_a)
        )
        monkeypatch.setattr(
            tmc_service, "save_catalog_revision", create_autospec(orig_save, side_effect=_save_a)
        )
        res_a = asyncio.run(
            update_catalog_model(
                actor_user_id="admin-1",
                model_key="alias-model-1",
                data=CatalogModelUpsertInput(
                    model_id="card-alias",
                    pricing=None,
                    dalton_meta={
                        "billing": "subscription",
                        "notional_pricing": {"input": 3.0, "output": 9.0},
                    },
                    expected_revision=15,
                ),
            )
        )
        assert res_a["revision"] == 16
        assert "var-alias" not in (state_a["payload"].get("providers", {}).get("models") or {})

        # Case B: another model still references canonical "card-canon" -> card must be preserved
        state_b = _make_state(with_second_model=True)

        async def _get_row_b(conn: Any = None, *, for_update: bool = False) -> dict[str, Any]:
            return copy.deepcopy(state_b)

        async def _save_b(
            conn: Any,
            *,
            expected_revision: int,
            action: str,
            target_keys: list[str],
            payload: dict[str, Any],
            diff_summary: dict[str, Any],
            side_effects: list[dict[str, Any]],
            actor_user_id: str,
            comment: str | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            state_b["revision"] = expected_revision + 1
            state_b["payload"] = copy.deepcopy(payload)
            return {
                "catalog": {"revision": state_b["revision"]},
                "revision_record": {"revision": state_b["revision"], "action": action},
            }

        monkeypatch.setattr(
            tmc_service, "get_catalog_row", create_autospec(orig_get_row, side_effect=_get_row_b)
        )
        monkeypatch.setattr(
            tmc_service, "save_catalog_revision", create_autospec(orig_save, side_effect=_save_b)
        )
        res_b = asyncio.run(
            update_catalog_model(
                actor_user_id="admin-1",
                model_key="alias-model-1",
                data=CatalogModelUpsertInput(
                    model_id="card-alias",
                    pricing=None,
                    dalton_meta={
                        "billing": "subscription",
                        "notional_pricing": {"input": 3.0, "output": 9.0},
                    },
                    expected_revision=15,
                ),
            )
        )
        assert res_b["revision"] == 16
        kept_cards = state_b["payload"]["providers"]["models"]["var-alias"]
        assert len(kept_cards) == 1
        assert kept_cards[0]["id"] == "card-canon"


class TestR8Fixes:
    """Regression suite for Batch 1 r8: test isolation, production cache invalidation,
    checkpoint resume ownership, subagent effort mapping, custom-model shadow priority,
    credential read failures, and Codex R7 high/medium items."""

    def test_r8_1_cache_isolation_and_provider_catalog_invalidation_on_revision_change(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.server.services.llm import provider_catalog as pc_mod
        from src.utils.tracking.core import calculate_cost_from_per_call_records

        # 1) Simulate a test caching a minimal fake revision-0 catalog, then resetting caches
        with monkeypatch.context() as mp:
            mp.setattr(
                "src.dalton.services.team_model_catalog.compiled._load_json_dict",
                lambda p: _base_upstream_models()
                if "models.json" in str(p)
                else (_base_upstream_providers() if "providers.json" in str(p) else {}),
            )
            mp.setattr(
                "src.dalton.llm.overlay.load_overlay_data",
                lambda path=None: _base_native_overlay(),
            )
            fake_rev0 = compile_catalog_from_payload({"schema": CATALOG_SCHEMA}, revision=0)
            cache_compiled_catalog(fake_rev0)
            assert "claude-opus-5-5" not in fake_rev0.model_config.llm_config

        reset_catalog_caches_for_tests()
        real_mc = LLM.get_model_config()
        assert "claude-opus-5-5" in real_mc.llm_config
        cost = calculate_cost_from_per_call_records(
            [
                {
                    "model_name": "claude-opus-5-5",
                    "provider": "anthropic",
                    "usage": {"input_tokens": 1000, "output_tokens": 500},
                }
            ]
        )
        assert cost["platform_cost"] > 0

        # 2) Production cache invalidation: provider_catalog() & key_candidates() refresh when
        #    LLM.get_model_config() moves to a new compiled revision inside the same worker.
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )
        cat_r1 = compile_catalog_from_payload({"schema": CATALOG_SCHEMA}, revision=1)
        cat_r2 = compile_catalog_from_payload(
            {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "provider_variants": {
                        "openai": {
                            "var-new-pc": {
                                "parent_provider": "openai",
                                "display_name": "Var New PC",
                                "sdk": "openai",
                                "billing": "cash",
                                "base_url": "https://var-new-pc.example.com/v1",
                            }
                        }
                    },
                    "models": {
                        "var-new-pc": [
                            {"id": "m-new", "name": "M New", "pricing": {"input": 1.0, "output": 2.0}}
                        ]
                    },
                },
                "models": {
                    "m-new": {
                        "model_id": "m-new",
                        "provider": "var-new-pc",
                        "display_name": "M New",
                        "visible": True,
                    }
                },
                "dalton_meta": {"m-new": {"billing": "cash", "tool_calling": True}},
            },
            revision=2,
        )
        current_cat = {"cat": cat_r1}
        orig_get_sync = compiled_mod.get_compiled_catalog_sync
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_sync, side_effect=lambda **_kw: current_cat["cat"]),
        )
        kc_r1 = pc_mod.key_candidates()
        assert "var-new-pc" not in kc_r1
        current_cat["cat"] = cat_r2
        kc_r2 = pc_mod.key_candidates()
        assert "var-new-pc" in kc_r2
        # Cache clear attribute remains callable for upstream callers/tests
        pc_mod.provider_catalog.cache_clear()
        pc_mod.key_candidates.cache_clear()

    def test_r8_2_checkpoint_resume_owner_direct_checkpoint_and_read_error_503(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import inspect
        from types import SimpleNamespace

        from src.server.app.threads import messaging
        from src.server.handlers import checkpoint_handler as ch_mod

        # 1) resolve_checkpoint_resume_run_id walks past source=="update" flush checkpoints
        #    and returns the checkpoint's owning run_id (Run A), never a later failed Run B.
        cp_run_a = SimpleNamespace(
            config={"configurable": {"thread_id": "t-r8", "checkpoint_id": "cp-a"}},
            metadata={"source": "loop", "run_id": "run-a-1111"},
            parent_config=None,
        )
        cp_flush = SimpleNamespace(
            config={"configurable": {"thread_id": "t-r8", "checkpoint_id": "cp-flush"}},
            metadata={"source": "update"},
            parent_config={"configurable": {"thread_id": "t-r8", "checkpoint_id": "cp-a"}},
        )

        class _FakeCP:
            async def aget_tuple(self, cfg: dict[str, Any]) -> Any:
                cid = (cfg.get("configurable") or {}).get("checkpoint_id")
                if cid == "cp-a":
                    return cp_run_a
                return cp_flush

        monkeypatch.setattr(ch_mod, "get_checkpointer", lambda: _FakeCP())
        assert (
            asyncio.run(ch_mod.resolve_checkpoint_resume_run_id("t-r8", "cp-flush")) == "run-a-1111"
        )
        assert asyncio.run(ch_mod.resolve_checkpoint_resume_run_id("t-r8", None)) == "run-a-1111"

        # 2) Verify _handle_send_message wires BOTH direct checkpoint resume and HITL resume
        #    through resolve_checkpoint_resume_run_id and raises HTTP 503 on read error.
        msg_src = inspect.getsource(messaging._handle_send_message)
        assert "is_direct_checkpoint_resume" in msg_src
        assert "resolve_checkpoint_resume_run_id" in msg_src
        assert "resume_verification_unavailable" in msg_src
        assert "status_code=503" in msg_src

    def test_r8_3_subagent_effort_mapping_before_verify_and_shared_model_id_actual_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from langchain_core.messages import ToolMessage

        from ptc_agent.agent.middleware.background_subagent import task_actions
        from ptc_agent.agent.middleware.background_subagent.subagent import run_on_by_type
        from ptc_agent.config.agent import TaskAsk, TaskModelRefused
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.snapshot import (
            _extract_primary_model_key_from_run,
        )
        from src.server.services.llm.task_model import TaskModelGate

        # 1) Shared API model_id: card_model returned None (models={}), but spec["model"] carries
        #    catalog_model="glm-5.3-cn" -> run_on_by_type records actual_model_key="glm-5.3-cn".
        fake_client = SimpleNamespace(metadata={"manifest_model": "glm-5.3-cn"})
        ro_map = run_on_by_type(
            [{"name": "research", "model": fake_client}],
            default_model=SimpleNamespace(metadata={"manifest_model": "glm-5.3"}),
            models={},
            claude_code=None,
        )
        assert "research" in ro_map
        assert ro_map["research"].model is None
        assert ro_map["research"].actual_model_key == "glm-5.3-cn"
        assert ro_map["research"].admission_kwargs()["actual_model_key"] == "glm-5.3-cn"

        # _extract_primary_model_key_from_run prioritizes actual_model_key over unmapped model_override
        assert (
            _extract_primary_model_key_from_run(
                {
                    "replay_facts": {
                        "model_override": "gemini-4-neon",
                        "actual_model_key": "gemini-4-neon-high-oauth",
                    }
                },
                is_subagent=True,
            )
            == "gemini-4-neon-high-oauth"
        )

        # 2) Subagent resume with Antigravity effort maps to the effort variant BEFORE verify_resume_route
        base_provs = _base_upstream_providers()
        base_provs["provider_config"]["google"] = {
            "display_name": "Google",
            "sdk": "google",
            "base_url": "https://generativelanguage.googleapis.com/v1",
            "env_key": "GOOGLE_API_KEY",
        }
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (base_provs if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )
        payload = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "google": {
                        "antigravity-oauth": {
                            "parent_provider": "google",
                            "display_name": "Antigravity OAuth",
                            "sdk": "google",
                            "billing": "subscription",
                            "base_url": "https://ag.example.com/v1",
                        }
                    }
                }
            },
            "models": {
                "gemini-4-neon-medium-oauth": {
                    "model_id": "gemini-4-neon-medium",
                    "provider": "antigravity-oauth",
                    "display_name": "Neon Medium",
                    "visible": True,
                },
                "gemini-4-neon-high-oauth": {
                    "model_id": "gemini-4-neon-high",
                    "provider": "antigravity-oauth",
                    "display_name": "Neon High",
                    "visible": True,
                },
            },
            "dalton_meta": {
                "gemini-4-neon-medium-oauth": {
                    "billing": "subscription",
                    "notional_pricing": {"input": 3.0, "output": 15.0},
                },
                "gemini-4-neon-high-oauth": {
                    "billing": "subscription",
                    "notional_pricing": {"input": 3.0, "output": 15.0},
                },
            },
        }
        cat = compile_catalog_from_payload(payload, revision=140, oauth_sync_enabled=True)
        orig_get_sync = compiled_mod.get_compiled_catalog_sync
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_sync, return_value=cat),
        )
        monkeypatch.setattr(LLM, "_model_config", cat.model_config)
        from src.dalton.database import team_model_catalog as tmc_db

        monkeypatch.setattr(
            tmc_db,
            "get_catalog_provider_credential",
            create_autospec(
                tmc_db.get_catalog_provider_credential,
                return_value={
                    "provider_slug": "antigravity-oauth",
                    "bound_base_url": "https://ag.example.com/v1",
                    "canonical_bound_url": "https://ag.example.com:443/v1",
                    "key_fingerprint": "fp-ag",
                },
            ),
        )
        snap = build_effective_catalog_snapshot(
            ["gemini-4-neon-high-oauth"],
            compiled=cat,
            variant_credentials={
                "antigravity-oauth": {
                    "bound_base_url": "https://ag.example.com/v1",
                    "canonical_bound_url": "https://ag.example.com:443/v1",
                    "key_fingerprint": "fp-ag",
                }
            },
        )
        assert snap is not None

        sentinel = TaskModelRefused(message="reached-resolve-model-after-verify", reason="ok")
        resolve_spy = create_autospec(task_actions._resolve_model, return_value=sentinel)
        monkeypatch.setattr(task_actions, "_resolve_model", resolve_spy)

        fake_gate = object.__new__(TaskModelGate)
        fake_gate._user_id = "u-1"
        fake_gate._model_pref = {}
        fake_gate._policy = None
        fake_gate._turn_model = "gpt-test"
        fake_gate._config = SimpleNamespace(
            subsidiary_llm_clients={},
            subagents=SimpleNamespace(resolved=None, definitions={}),
        )
        monkeypatch.setattr(fake_gate, "_definition", lambda _st: None)
        monkeypatch.setattr(fake_gate, "_own_model", lambda _st: "gemini-4-neon-medium-oauth")

        # effective_model_key maps gemini-4-neon-medium-oauth + effort="high" -> gemini-4-neon-high-oauth
        assert (
            asyncio.run(
                fake_gate.effective_model_key(
                    TaskAsk(model="gemini-4-neon-medium-oauth", effort="high"), "research"
                )
            )
            == "gemini-4-neon-high-oauth"
        )

        fake_task = SimpleNamespace(
            task_id="sub-ag-1",
            display_id="Task-sub-ag-1",
            subagent_type="research",
            is_pending=False,
            cancelled=False,
            terminal_status=None,
            asyncio_task=None,
            result=None,
            ask=TaskAsk(model="gemini-4-neon-medium-oauth", effort="high"),
            lock=asyncio.Lock(),
        )

        async def _resolve_ag_task(*_a: Any, **_kw: Any) -> Any:
            return fake_task

        fake_mw = SimpleNamespace(
            _resolve_or_error=_resolve_ag_task,
            subagent_types={"research"},
            _resume_claims=set(),
            registry=SimpleNamespace(
                get_by_id=lambda tid: fake_task if tid == "sub-ag-1" else None,
                run_ledger=object(),
            ),
            task_models=fake_gate,
            namespace_owner=None,
        )
        fake_request = SimpleNamespace(
            tool_call={"id": "tc-ag-1", "args": {"action": "resume", "task_id": "sub-ag-1"}},
            state={},
        )
        pred_row = {
            "task_run_id": "99999999-9999-4999-8999-999999999999",
            "thread_id": "thread-r8-3",
            "status": "completed",
            "replay_facts": {
                "model_override": "gemini-4-neon-medium-oauth",
                "effort_override": "high",
                "actual_model_key": "gemini-4-neon-high-oauth",
                "effective_catalog_snapshot": snap,
            },
        }
        monkeypatch.setattr(
            task_actions,
            "_read_latest_run",
            create_autospec(task_actions._read_latest_run, return_value=pred_row),
        )
        msg = asyncio.run(
            task_actions._handle_resume(
                fake_mw,
                fake_request,
                handler=lambda _req: None,
                target_task_id="sub-ag-1",
                subagent_type=None,
                description="resume sub-ag-1",
                prompt="continue",
                args={"action": "resume", "task_id": "sub-ag-1"},
                parent_thread_id="thread-r8-3",
                tool_call_id="tc-ag-1",
                current_run_id="parent-run-ag-1",
                ask=TaskAsk(),
            )
        )
        assert isinstance(msg, ToolMessage)
        assert "catalog_route_changed" not in str(msg.content)
        assert resolve_spy.call_count == 1

    def test_r8_4_same_name_custom_model_allowed_after_team_model_retirement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.team_model_catalog import service as tmc_service

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )
        retired_cat = compile_catalog_from_payload(
            {
                "schema": CATALOG_SCHEMA,
                "retired": [{"key": "gpt-test", "retired_at": "2026-11-25T00:00:00Z"}],
            },
            revision=20,
        )
        assert "gpt-test" in retired_cat.team_retired_keys
        monkeypatch.setattr(
            tmc_service,
            "get_compiled_catalog_async",
            create_autospec(tmc_service.get_compiled_catalog_async, return_value=retired_cat),
        )

        # User with personal custom model named "gpt-test" is ALLOWED even though team retired "gpt-test"
        pref_with_same_name = {
            "custom_models": [
                {"name": "gpt-test", "provider": "openai", "model_id": "gpt-test-custom"}
            ]
        }
        compiled_ok = asyncio.run(
            assert_model_not_retired_under_lock(
                None,
                "gpt-test",
                field="llm_model",
                user_id="u-custom",
                model_pref=pref_with_same_name,
                allow_custom=True,
            )
        )
        assert compiled_ok is retired_cat

        # User with personal custom provider named "gpt-test" is also ALLOWED
        pref_with_cp = {
            "custom_providers": [{"name": "gpt-test", "parent_provider": "openai"}]
        }
        compiled_cp_ok = asyncio.run(
            assert_model_not_retired_under_lock(
                None,
                "gpt-test",
                field="model",
                user_id="u-cp",
                model_pref=pref_with_cp,
                allow_custom=True,
            )
        )
        assert compiled_cp_ok is retired_cat

        # User WITHOUT a custom model/provider named "gpt-test" is still rejected with 400 model_retired
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                assert_model_not_retired_under_lock(
                    None,
                    "gpt-test",
                    field="llm_model",
                    user_id="u-plain",
                    model_pref={},
                    allow_custom=True,
                )
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == "model_retired"

    def test_r8_5_credential_read_failure_skips_incomplete_snapshot_and_live_check_returns_503(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.database import team_model_catalog as tmc_db
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.snapshot import (
            resolve_live_route_fingerprint,
        )

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )
        payload = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "openai": {
                        "var-cred-fail": {
                            "parent_provider": "openai",
                            "display_name": "Var Cred Fail",
                            "sdk": "openai",
                            "billing": "cash",
                            "base_url": "https://var-cred-fail.example.com/v1",
                        }
                    }
                },
                "models": {
                    "var-cred-fail": [
                        {"id": "m-cf", "name": "M CF", "pricing": {"input": 1.0, "output": 2.0}}
                    ]
                },
            },
            "models": {
                "m-cf": {
                    "model_id": "m-cf",
                    "provider": "var-cred-fail",
                    "display_name": "M CF",
                    "visible": True,
                }
            },
            "dalton_meta": {"m-cf": {"billing": "cash", "tool_calling": True}},
        }
        cat = compile_catalog_from_payload(payload, revision=150)

        # 1) build_effective_catalog_snapshot returns None (skips writing incomplete snapshot) when
        #    credential read raises for a catalog-defined variant.
        monkeypatch.setattr(
            tmc_db,
            "list_catalog_provider_credentials_sync",
            create_autospec(
                tmc_db.list_catalog_provider_credentials_sync,
                side_effect=RuntimeError("pg connection reset"),
            ),
        )
        assert build_effective_catalog_snapshot(["m-cf"], compiled=cat) is None
        # Non-variant model does not depend on variant credentials and still succeeds
        plain_snap = build_effective_catalog_snapshot(["gpt-test"], compiled=cat)
        assert plain_snap is not None and "gpt-test" in plain_snap["models"]

        # 2) resolve_live_route_fingerprint raises 503 on DB error instead of false catalog_route_changed
        orig_get_sync = compiled_mod.get_compiled_catalog_sync
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_sync, return_value=cat),
        )
        monkeypatch.setattr(LLM, "_model_config", cat.model_config)
        monkeypatch.setattr(
            tmc_db,
            "get_catalog_provider_credential",
            create_autospec(
                tmc_db.get_catalog_provider_credential,
                side_effect=RuntimeError("pg timeout"),
            ),
        )
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(resolve_live_route_fingerprint("m-cf"))
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["code"] == "catalog_credential_check_unavailable"

    def test_r8_6_toctou_exact_model_id_tasks_restart_pricing_order_and_complete_byok_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.database import team_model_catalog as tmc_db
        from src.dalton.llm import complete as complete_mod
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.snapshot import (
            RouteFingerprint,
            _extract_cash_pricing_card,
            bind_verified_resume_variant_binding,
            verify_resume_route,
        )
        from src.dalton.services.team_model_catalog.transport import (
            resolve_catalog_variant_binding_async,
        )
        from src.server.services.llm import clients as llm_clients
        from src.server.services.llm import user_models
        from src.server.services.report_back import restart as restart_mod

        # A) Variant credential TOCTOU between verify_resume_route and resolve_catalog_variant_binding_async
        monkeypatch.setattr(
            tmc_db,
            "get_catalog_provider_credential",
            create_autospec(
                tmc_db.get_catalog_provider_credential,
                return_value={
                    "provider_slug": "var-toctou",
                    "api_key": "sk-rotated",
                    "bound_base_url": "https://var-toctou.example.com/v1",
                    "canonical_bound_url": "https://var-toctou.example.com:443/v1",
                    "key_fingerprint": "fp-rotated",
                },
            ),
        )
        verified_fp = RouteFingerprint(
            model_key="m-toctou",
            provider="var-toctou",
            model_id="m-toctou",
            bound_base_url="https://var-toctou.example.com:443/v1",
            key_fingerprint="fp-original",
            billing="cash",
        )
        with bind_verified_resume_variant_binding(verified_fp):
            with pytest.raises(HTTPException) as exc_toctou:
                asyncio.run(resolve_catalog_variant_binding_async("var-toctou", model_key="m-toctou"))
            assert exc_toctou.value.status_code == 400
            assert exc_toctou.value.detail["code"] == "catalog_route_changed"

        # B) Exact model_id comparison: gpt-6.2-sol vs gpt-6-2-sol refuses resume
        snap_exact = {
            "schema": "dalton-turn-catalog-snapshot/1",
            "catalog_revision": 1,
            "models": {
                "m-exact": {
                    "model_key": "m-exact",
                    "provider": "openai",
                    "model_id": "gpt-6.2-sol",
                    "billing": "cash",
                    "provider_info": {"base_url": "https://api.openai.com/v1"},
                }
            },
            "variants": {},
        }
        with pytest.raises(HTTPException) as exc_mid:
            asyncio.run(
                verify_resume_route(
                    "run-mid",
                    RouteFingerprint(
                        model_key="m-exact",
                        provider="openai",
                        model_id="gpt-6-2-sol",
                        bound_base_url="https://api.openai.com:443/v1",
                        billing="cash",
                    ),
                    snapshot=snap_exact,
                )
            )
        assert exc_mid.value.status_code == 400
        assert exc_mid.value.detail["code"] == "catalog_route_changed"

        # C) Tasks-only restart (interrupted=False, tasks non-empty) sets source_run_id=None
        import inspect

        rv_src = inspect.getsource(restart_mod.resume_verdict)
        assert "source_run_id=(str(run_id) if interrupted else None)" in rv_src

        # D) Provider-before-parent pricing lookup parity between find_model_pricing and snapshot
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )
        pricing_payload = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "openai": {
                        "var-price-parity": {
                            "parent_provider": "openai",
                            "display_name": "Var Price Parity",
                            "sdk": "openai",
                            "billing": "cash",
                            "base_url": "https://var-price.example.com/v1",
                        }
                    }
                },
                "models": {
                    "openai": [
                        {
                            "id": "gpt-6.2-sol",
                            "name": "Parent Exact",
                            "pricing": {"input": 99.0, "output": 199.0},
                        }
                    ],
                    "var-price-parity": [
                        {
                            "id": "gpt-6-2-sol",
                            "name": "Variant Hyphen",
                            "pricing": {"input": 2.0, "output": 8.0},
                        }
                    ],
                },
            },
            "models": {
                "m-parity": {
                    "model_id": "gpt-6.2-sol",
                    "provider": "var-price-parity",
                    "display_name": "M Parity",
                    "visible": True,
                }
            },
            "dalton_meta": {"m-parity": {"billing": "cash", "tool_calling": True}},
        }
        cat_parity = compile_catalog_from_payload(pricing_payload, revision=160)
        snap_card = _extract_cash_pricing_card(
            cat_parity.model_config, "var-price-parity", "gpt-6.2-sol"
        )
        live_card = find_model_pricing(
            "gpt-6.2-sol",
            "var-price-parity",
            model_config=cat_parity.model_config,
            model_key="m-parity",
        )
        assert snap_card is not None and live_card is not None
        assert snap_card["input"] == 2.0
        assert live_card["input"] == 2.0

        # E) complete.py resolves actual BYOK credential route (z-ai-cn -> z-ai-cn-coding)
        orig_get_sync = compiled_mod.get_compiled_catalog_sync
        cat_zai = compile_catalog_from_payload(
            {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "provider_variants": {
                        "openai": {
                            "z-ai-cn": {
                                "parent_provider": "openai",
                                "display_name": "Z-AI CN",
                                "sdk": "openai",
                                "billing": "cash",
                                "base_url": "https://z-ai-cn.example.com/v1",
                            },
                            "z-ai-cn-coding": {
                                "parent_provider": "openai",
                                "display_name": "Z-AI CN Coding",
                                "sdk": "openai",
                                "billing": "subscription",
                                "base_url": "https://z-ai-cn-coding.example.com/v1",
                            },
                        }
                    }
                },
            },
            revision=161,
        )
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(orig_get_sync, return_value=cat_zai),
        )
        monkeypatch.setattr(LLM, "_model_config", cat_zai.model_config)
        custom_entry = {"name": "my-glm", "provider": "z-ai-cn", "model_id": "glm-5"}
        monkeypatch.setattr(
            user_models,
            "classify_model",
            create_autospec(
                user_models.classify_model,
                return_value=(user_models.ModelSource.CUSTOM, custom_entry),
            ),
        )
        monkeypatch.setattr(
            llm_clients,
            "byok_key",
            create_autospec(
                llm_clients.byok_key,
                return_value=llm_clients.OwnKeyRoute(
                    key={"api_key": "k-coding"},
                    base_url="https://z-ai-cn-coding.example.com/v1",
                    provider="z-ai-cn-coding",
                    custom_config={**custom_entry, "provider": "z-ai-cn-coding"},
                    holding="z-ai-cn-coding",
                ),
            ),
        )
        billing_type, eff_prov, eff_mid, eff_pprov = asyncio.run(
            complete_mod._resolve_model_pricing_identity(
                "my-glm",
                user_id="u-coding",
            )
        )
        assert billing_type == "oauth"
        assert eff_prov == "z-ai-cn-coding"
        assert eff_mid == "glm-5"
        assert eff_pprov == "z-ai-cn-coding"

    def test_r9_1_subagent_personal_custom_model_shadowing_team_catalog_not_rejected_on_resume(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from langchain_core.messages import ToolMessage

        from ptc_agent.agent.middleware.background_subagent import task_actions
        from ptc_agent.config.agent import TaskAsk, TaskModelRefused
        from src.dalton.database import team_model_catalog as tmc_db
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.llms.llm import LLM as LLMFactory
        from src.server.database import workspace as ws_db
        from src.server.database.runs import subagent_runs
        from src.server.services.llm import user_models

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        payload = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "openai": {
                        "sub-team-gw": {
                            "parent_provider": "openai",
                            "display_name": "Sub Team GW",
                            "sdk": "openai",
                            "billing": "cash",
                            "base_url": "https://sub-team-gw.example.com/v1",
                        }
                    }
                },
                "models": {
                    "sub-team-gw": [
                        {
                            "id": "team-shared-id",
                            "name": "Team Shared",
                            "pricing": {"input": 1.0, "output": 2.0},
                        }
                    ]
                },
            },
            "models": {
                "shared-alias": {
                    "model_id": "team-shared-id",
                    "provider": "sub-team-gw",
                    "display_name": "Shared Alias",
                    "visible": True,
                }
            },
            "dalton_meta": {"shared-alias": {"billing": "cash", "tool_calling": True}},
        }
        cat = compile_catalog_from_payload(payload, revision=170)
        cache_compiled_catalog(cat)
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(compiled_mod.get_compiled_catalog_sync, return_value=cat),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", cat.model_config)
        monkeypatch.setattr(
            tmc_db,
            "list_catalog_provider_credentials_sync",
            create_autospec(
                tmc_db.list_catalog_provider_credentials_sync,
                return_value={
                    "sub-team-gw": {
                        "provider_slug": "sub-team-gw",
                        "bound_base_url": "https://sub-team-gw.example.com/v1",
                        "canonical_bound_url": "https://sub-team-gw.example.com:443/v1",
                        "key_fingerprint": "fp-sub-team",
                    }
                },
            ),
        )
        monkeypatch.setattr(
            tmc_db,
            "get_catalog_provider_credential",
            create_autospec(
                tmc_db.get_catalog_provider_credential,
                return_value={
                    "provider_slug": "sub-team-gw",
                    "bound_base_url": "https://sub-team-gw.example.com/v1",
                    "canonical_bound_url": "https://sub-team-gw.example.com:443/v1",
                    "key_fingerprint": "fp-sub-team",
                },
            ),
        )
        monkeypatch.setattr(
            ws_db,
            "lock_run_workspace",
            create_autospec(ws_db.lock_run_workspace, return_value=None),
        )

        personal_entry = {
            "name": "shared-alias",
            "provider": "openai",
            "model_id": "gpt-personal-only",
            "base_url": "https://personal.example.com/v1",
        }
        team_entry = cat.model_config.get_model_config("shared-alias")

        async def _fake_classify(
            uid: str,
            model_name: str,
            _pref_cache: dict[str, Any] | None = None,
            ignore_custom_models: bool = False,
        ) -> tuple[str, dict[str, Any]]:
            if not ignore_custom_models and uid == "u-personal" and model_name == "shared-alias":
                return user_models.ModelSource.CUSTOM, personal_entry
            if model_name == "shared-alias" and isinstance(team_entry, dict):
                return user_models.ModelSource.SYSTEM, team_entry
            return user_models.ModelSource.UNKNOWN, {}

        monkeypatch.setattr(
            user_models,
            "classify_model",
            create_autospec(user_models.classify_model, side_effect=_fake_classify),
        )

        active_user = {"uid": "u-personal"}

        class _FakeCursor:
            def __init__(self) -> None:
                self._next_row: dict[str, Any] | None = None

            async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
                if "AS user_id" in sql:
                    self._next_row = {"user_id": active_user["uid"]}
                elif "INSERT INTO subagent_runs" in sql:
                    facts_bind = params[8]
                    facts_dict = (
                        dict(facts_bind.obj)
                        if facts_bind is not None and hasattr(facts_bind, "obj")
                        else {}
                    )
                    self._next_row = {
                        "task_run_id": params[0],
                        "thread_id": params[1],
                        "task_id": params[2],
                        "replay_facts": facts_dict,
                        "user_id": active_user["uid"],
                    }
                else:
                    self._next_row = None

            async def fetchone(self) -> dict[str, Any] | None:
                return self._next_row

        class _FakeConn:
            @asynccontextmanager
            async def transaction(self) -> Any:
                yield self

            @asynccontextmanager
            async def cursor(self, **_kw: Any) -> Any:
                yield _FakeCursor()

        # 1) Personal custom model shadowing team catalog model -> writes valid empty snapshot
        #    with excluded_custom_keys so later load_run_catalog_snapshot never backfills team route
        row_personal = asyncio.run(
            subagent_runs.start_task_run(
                task_run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
                thread_id="thread-r9-1",
                task_id="task-r9-personal",
                cause="init",
                actual_model_key="shared-alias",
                model={"id": "shared-alias", "display_name": "Shared Alias"},
                model_override="shared-alias",
                conn=_FakeConn(),
            )
        )
        personal_snap = row_personal["replay_facts"].get("effective_catalog_snapshot")
        assert isinstance(personal_snap, dict)
        assert personal_snap.get("schema") == TURN_SNAPSHOT_SCHEMA
        assert personal_snap.get("models") == {}
        assert personal_snap.get("excluded_custom_keys") == ["shared-alias"]
        assert row_personal["replay_facts"].get("catalog_revision") == 170

        # 2) Control: user without personal shadow resolves to team catalog -> snapshot IS written
        active_user["uid"] = "u-team"
        row_team = asyncio.run(
            subagent_runs.start_task_run(
                task_run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
                thread_id="thread-r9-1",
                task_id="task-r9-team",
                cause="init",
                actual_model_key="shared-alias",
                model={"id": "shared-alias", "display_name": "Shared Alias"},
                model_override="shared-alias",
                conn=_FakeConn(),
            )
        )
        team_snap = row_team["replay_facts"].get("effective_catalog_snapshot")
        assert isinstance(team_snap, dict)
        assert "shared-alias" in (team_snap.get("models") or {})

        # 3) Resuming u-personal's subagent with unchanged personal custom model is NOT rejected
        sentinel_refusal = TaskModelRefused(
            message="sentinel-reached-resolve-model", reason="sentinel"
        )
        resolve_spy = create_autospec(task_actions._resolve_model, return_value=sentinel_refusal)
        monkeypatch.setattr(task_actions, "_resolve_model", resolve_spy)
        monkeypatch.setattr(
            task_actions,
            "_read_latest_run",
            create_autospec(task_actions._read_latest_run, return_value=row_personal),
        )

        fake_task = SimpleNamespace(
            task_id="task-r9-personal",
            display_id="Task-task-r9-personal",
            subagent_type="research",
            is_pending=False,
            cancelled=False,
            terminal_status=None,
            asyncio_task=None,
            result=None,
            ask=TaskAsk(model="shared-alias"),
            lock=asyncio.Lock(),
        )

        async def _resolve_ok_task(*_a: Any, **_kw: Any) -> Any:
            return fake_task

        fake_mw = SimpleNamespace(
            _resolve_or_error=_resolve_ok_task,
            subagent_types={"research"},
            _resume_claims=set(),
            registry=SimpleNamespace(
                get_by_id=lambda tid: fake_task if tid == "task-r9-personal" else None,
                run_ledger=object(),
            ),
            task_models=SimpleNamespace(
                _own_model=lambda _st: "shared-alias",
                _user_id="u-personal",
                _model_pref={"custom_models": [personal_entry]},
            ),
            namespace_owner=None,
        )
        fake_request = SimpleNamespace(
            tool_call={
                "id": "tc-r9-personal",
                "args": {"action": "resume", "task_id": "task-r9-personal"},
            },
            state={},
        )

        res = asyncio.run(
            task_actions._handle_resume(
                fake_mw,
                fake_request,
                handler=lambda _req: None,
                target_task_id="task-r9-personal",
                subagent_type=None,
                description="resume personal shadow",
                prompt="continue",
                args={"action": "resume", "task_id": "task-r9-personal"},
                parent_thread_id="thread-r9-1",
                tool_call_id="tc-r9-personal",
                current_run_id="parent-run-r9-1",
                ask=TaskAsk(),
            )
        )
        assert isinstance(res, ToolMessage)
        assert resolve_spy.call_count == 1
        assert "sentinel" in str(res.content)

    def test_r9_2_retry_explicit_checkpoint_verifies_checkpoint_owner_and_default_verifies_failed_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services import automation_thread_model as atm_mod
        from src.dalton.services.team_model_catalog import snapshot as snap_mod
        from src.dalton.services.team_model_catalog.snapshot import (
            RouteFingerprint,
            get_active_resume_verify_target,
            verify_resume_route,
        )
        from src.dalton import sharing as sharing_mod
        from src.server.app import setup as setup_mod
        from src.server.app.threads import messaging
        from src.server.database.conversation import queries as queries_db
        from src.server.database.runs import lifecycle as tl_db
        from src.server.dependencies import cli_approval
        from src.server.dependencies.principal import Principal
        from src.server.dependencies.usage_limits import ChatAuthResult
        from src.server.handlers import checkpoint_handler as ch_mod
        from src.server.models.chat import ChatRequest
        from src.server.models.workflow import RetryRequest
        from src.server.services import turn_runtime as tr_mod
        from src.server.services.runs import admission as adm_mod
        from src.server.services.runs import pending_input as pi_mod
        from src.server.services.shared_workspaces.access import Access
        from src.server.services.shared_workspaces.turn import TurnIdentity

        failed_run_2 = {
            "conversation_response_id": "22222222-2222-4222-8222-222222222222",
            "conversation_thread_id": "thread-r9-retry",
            "turn_index": 2,
            "status": "error",
            "metadata": {"llm_model": "model-new"},
        }
        failed_run_2_pending_raw = {
            **failed_run_2,
            "metadata": {
                "llm_model": "model-new",
                "pending_input": {"checkpoint_id": "cp-fork", "replay": False},
            },
        }
        auth = ChatAuthResult(
            user_id="u-r9-retry",
            is_byok=False,
            has_oauth=True,
            access_tier=0,
            burst_slot_id="slot-r9",
        )

        # Part A: retry_thread forwards retry_checkpoint_id ONLY when body.checkpoint_id is
        # explicitly given in the checkpoint-replay branch (not on default retry or raw-input rerun).
        mock_scope = create_autospec(adm_mod.RunScope, instance=True)
        monkeypatch.setattr(
            adm_mod,
            "RunScope",
            create_autospec(adm_mod.RunScope, return_value=mock_scope),
        )
        monkeypatch.setattr(
            cli_approval,
            "admit_retry",
            create_autospec(cli_approval.admit_retry, return_value=None),
        )
        monkeypatch.setattr(
            messaging,
            "thread_access",
            create_autospec(messaging.thread_access, return_value=Access("u-r9-retry")),
        )
        monkeypatch.setattr(
            messaging,
            "_assert_stream_transport_ready",
            create_autospec(messaging._assert_stream_transport_ready, return_value=None),
        )
        monkeypatch.setattr(
            ch_mod,
            "get_retry_checkpoint",
            create_autospec(
                ch_mod.get_retry_checkpoint,
                side_effect=lambda _tid, cp=None: cp or "cp-auto-latest",
            ),
        )
        monkeypatch.setattr(
            pi_mod,
            "retried_wake_fields",
            create_autospec(pi_mod.retried_wake_fields, return_value={}),
        )
        monkeypatch.setattr(
            queries_db,
            "get_query_at_turn",
            create_autospec(
                queries_db.get_query_at_turn,
                return_value={"content": "retry raw input", "type": "user", "metadata": {}},
            ),
        )

        async def _limit_fn(_uid: str) -> ChatAuthResult:
            return auth

        orig_handle_send = messaging._handle_send_message
        send_spy = create_autospec(orig_handle_send, return_value="ok")
        monkeypatch.setattr(messaging, "_handle_send_message", send_spy)

        # A1) Explicit body.checkpoint_id -> forwards retry_checkpoint_id="cp-run-1"
        real_get_latest_attempt = tl_db.get_latest_attempt
        monkeypatch.setattr(
            tl_db,
            "get_latest_attempt",
            create_autospec(real_get_latest_attempt, return_value=failed_run_2),
        )
        asyncio.run(
            messaging.retry_thread(
                "thread-r9-retry",
                Principal("u-r9-retry", "session"),
                _limit_fn,
                RetryRequest(workspace_id="ws-r9", checkpoint_id="cp-run-1"),
            )
        )
        assert send_spy.call_args.kwargs["retry_of_run_id"] == failed_run_2["conversation_response_id"]
        assert send_spy.call_args.kwargs["resume_source_run"] == failed_run_2
        assert send_spy.call_args.kwargs["retry_checkpoint_id"] == "cp-run-1"

        # A2) No explicit body.checkpoint_id -> omits retry_checkpoint_id
        send_spy.reset_mock()
        asyncio.run(
            messaging.retry_thread(
                "thread-r9-retry",
                Principal("u-r9-retry", "session"),
                _limit_fn,
                RetryRequest(workspace_id="ws-r9"),
            )
        )
        assert send_spy.call_args.kwargs["retry_of_run_id"] == failed_run_2["conversation_response_id"]
        assert send_spy.call_args.kwargs["resume_source_run"] == failed_run_2
        assert "retry_checkpoint_id" not in send_spy.call_args.kwargs

        # A3) Raw-input resubmit (pending_input.replay=False) -> omits retry_checkpoint_id even if body carries one
        send_spy.reset_mock()
        monkeypatch.setattr(
            tl_db,
            "get_latest_attempt",
            create_autospec(real_get_latest_attempt, return_value=failed_run_2_pending_raw),
        )
        asyncio.run(
            messaging.retry_thread(
                "thread-r9-retry",
                Principal("u-r9-retry", "session"),
                _limit_fn,
                RetryRequest(workspace_id="ws-r9", checkpoint_id="cp-run-1"),
            )
        )
        assert send_spy.call_args.kwargs["retry_of_run_id"] == failed_run_2_pending_raw["conversation_response_id"]
        assert send_spy.call_args.kwargs["resume_source_run"] == failed_run_2_pending_raw
        assert "retry_checkpoint_id" not in send_spy.call_args.kwargs

        # Part B: _handle_send_message resolves checkpoint owner via resolve_checkpoint_resume_run_id
        # when retry_checkpoint_id is set, and validates failed run when omitted.
        monkeypatch.setattr(messaging, "_handle_send_message", orig_handle_send)
        monkeypatch.setattr(
            messaging,
            "get_thread_auth_meta",
            create_autospec(
                messaging.get_thread_auth_meta,
                return_value={
                    "user_id": "u-r9-retry",
                    "workspace_id": "ws-r9",
                    "workspace_owner_id": "u-r9-retry",
                },
            ),
        )
        monkeypatch.setattr(
            messaging,
            "_settle_against_branch",
            create_autospec(
                messaging._settle_against_branch,
                side_effect=lambda _tid, r, _uid, **_kw: r,
            ),
        )
        monkeypatch.setattr(setup_mod, "agent_config", object(), raising=False)
        monkeypatch.setattr(
            sharing_mod,
            "resolve_colleague_turn_admission",
            create_autospec(
                sharing_mod.resolve_colleague_turn_admission,
                return_value=({"workspace_id": "ws-r9", "user_id": "u-r9-retry"}, "web", False),
            ),
        )
        mock_route = create_autospec(tr_mod.TurnRoute, instance=True)
        mock_route.workspace_id = "ws-r9"
        mock_route.owner_id = "u-r9-retry"
        mock_route.role = "analyst"
        monkeypatch.setattr(
            tr_mod,
            "turn_route",
            create_autospec(tr_mod.turn_route, return_value=mock_route),
        )
        mock_identity = create_autospec(TurnIdentity, instance=True)
        monkeypatch.setattr(
            TurnIdentity,
            "of",
            create_autospec(TurnIdentity.of, return_value=mock_identity),
        )

        captured_targets: list[Any] = []

        async def _capture_target(*_a: Any, **_kw: Any) -> Any:
            captured_targets.append(get_active_resume_verify_target())
            raise HTTPException(status_code=418, detail="captured_resume_target")

        monkeypatch.setattr(
            atm_mod,
            "resolve_turn_config_with_automation_inheritance",
            create_autospec(
                atm_mod.resolve_turn_config_with_automation_inheritance,
                side_effect=_capture_target,
            ),
        )

        run_1_id = "11111111-1111-4111-8111-111111111111"
        real_resolve_checkpoint_resume_run_id = ch_mod.resolve_checkpoint_resume_run_id
        resolve_cp_spy = create_autospec(
            real_resolve_checkpoint_resume_run_id,
            return_value=run_1_id,
        )
        monkeypatch.setattr(ch_mod, "resolve_checkpoint_resume_run_id", resolve_cp_spy)

        # B1) Explicit retry_checkpoint_id="cp-run-1" -> verifies Run 1 (clears Run 2 row)
        with pytest.raises(HTTPException) as exc_b1:
            asyncio.run(
                messaging._handle_send_message(
                    ChatRequest(workspace_id="ws-r9", messages=[], checkpoint_id="cp-run-1"),
                    auth,
                    "thread-r9-retry",
                    retry_of_run_id=failed_run_2["conversation_response_id"],
                    resume_source_run=failed_run_2,
                    retry_checkpoint_id="cp-run-1",
                )
            )
        assert exc_b1.value.status_code == 418
        resolve_cp_spy.assert_awaited_once_with("thread-r9-retry", "cp-run-1")
        target_b1 = captured_targets[-1]
        assert target_b1 is not None
        assert target_b1.run_id == run_1_id
        assert target_b1.run_row is None

        # Verify that running verify_resume_route against target_b1 compares Run 1's snapshot
        # (and rejects when Run 1's catalog route changed, even though failed Run 2 did not).
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )
        cat_r1 = compile_catalog_from_payload(
            {
                "schema": CATALOG_SCHEMA,
                "providers": {
                    "provider_variants": {
                        "openai": {
                            "r9-gw": {
                                "parent_provider": "openai",
                                "display_name": "R9 GW",
                                "sdk": "openai",
                                "billing": "cash",
                                "base_url": "https://r9-gw.example.com/v1",
                            }
                        }
                    },
                    "models": {
                        "r9-gw": [
                            {"id": "r9-id-v1", "name": "R9 V1", "pricing": {"input": 1.0, "output": 2.0}}
                        ]
                    },
                },
                "models": {
                    "r9-team-model": {
                        "model_id": "r9-id-v1",
                        "provider": "r9-gw",
                        "display_name": "R9 Team Model",
                        "visible": True,
                    }
                },
                "dalton_meta": {"r9-team-model": {"billing": "cash", "tool_calling": True}},
            },
            revision=171,
        )
        snap_r1 = build_effective_catalog_snapshot(
            ["r9-team-model"],
            compiled=cat_r1,
            variant_credentials={
                "r9-gw": {
                    "bound_base_url": "https://r9-gw.example.com/v1",
                    "canonical_bound_url": "https://r9-gw.example.com:443/v1",
                    "key_fingerprint": "fp-r9-v1",
                }
            },
        )
        run_1_row = {
            "conversation_response_id": run_1_id,
            "conversation_thread_id": "thread-r9-retry",
            "metadata": {
                "llm_model": "r9-team-model",
                "effective_catalog_snapshot": snap_r1,
            },
        }
        monkeypatch.setattr(
            snap_mod,
            "_fetch_persisted_run_row",
            create_autospec(snap_mod._fetch_persisted_run_row, return_value=run_1_row),
        )
        with pytest.raises(HTTPException) as exc_route_changed:
            asyncio.run(
                verify_resume_route(
                    target_b1.run_id,
                    RouteFingerprint(
                        model_key="r9-team-model",
                        provider="r9-gw",
                        model_id="r9-id-v2-mutated",
                        bound_base_url="https://r9-gw.example.com:443/v1",
                        key_fingerprint="fp-r9-v1",
                        billing="cash",
                    ),
                    thread_id=target_b1.thread_id,
                    is_subagent=False,
                    run_row=target_b1.run_row,
                )
            )
        assert exc_route_changed.value.status_code == 400
        assert exc_route_changed.value.detail["code"] == "catalog_route_changed"

        # B2) Default retry (no retry_checkpoint_id) -> validates failed Run 2 without querying checkpointer
        resolve_cp_spy.reset_mock()
        with pytest.raises(HTTPException) as exc_b2:
            asyncio.run(
                messaging._handle_send_message(
                    ChatRequest(workspace_id="ws-r9", messages=[], checkpoint_id="cp-auto-latest"),
                    auth,
                    "thread-r9-retry",
                    retry_of_run_id=failed_run_2["conversation_response_id"],
                    resume_source_run=failed_run_2,
                )
            )
        assert exc_b2.value.status_code == 418
        assert resolve_cp_spy.await_count == 0
        target_b2 = captured_targets[-1]
        assert target_b2 is not None
        assert target_b2.run_id == failed_run_2["conversation_response_id"]
        assert target_b2.run_row == failed_run_2

        # B3) Checkpointer lookup failure on explicit retry checkpoint -> fail closed with HTTP 503
        monkeypatch.setattr(
            ch_mod,
            "resolve_checkpoint_resume_run_id",
            create_autospec(
                real_resolve_checkpoint_resume_run_id,
                side_effect=RuntimeError("checkpointer unavailable"),
            ),
        )
        with pytest.raises(HTTPException) as exc_b3:
            asyncio.run(
                messaging._handle_send_message(
                    ChatRequest(workspace_id="ws-r9", messages=[], checkpoint_id="cp-run-1"),
                    auth,
                    "thread-r9-retry",
                    retry_of_run_id=failed_run_2["conversation_response_id"],
                    resume_source_run=failed_run_2,
                    retry_checkpoint_id="cp-run-1",
                )
            )
        assert exc_b3.value.status_code == 503
        assert exc_b3.value.detail["code"] == "resume_verification_unavailable"


# ---------------------------------------------------------------------------
# Group 11: R11 fixes — empty snapshot prevents backfill overwrite &
#           start_task_run classifies outside pool connection / transaction
# ---------------------------------------------------------------------------


class TestR11Fixes:
    def test_r11_1_subagent_personal_custom_empty_snapshot_survives_load_backfill_settlement_and_resume(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from langchain_core.messages import ToolMessage

        from ptc_agent.agent.middleware.background_subagent import task_actions
        from ptc_agent.config.agent import TaskAsk, TaskModelRefused
        from src.dalton.database import team_model_catalog as tmc_db
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog import snapshot as snap_mod
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.dalton.services.team_model_catalog.snapshot import (
            RouteFingerprint,
            load_run_catalog_snapshot,
            verify_resume_route,
        )
        from src.llms.llm import LLM as LLMFactory
        from src.server.database import workspace as ws_db
        from src.server.database.runs import subagent_runs
        from src.server.services.llm import user_models
        from src.utils.tracking.core import calculate_cost_from_per_call_records

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        payload = {
            "schema": CATALOG_SCHEMA,
            "providers": {
                "provider_variants": {
                    "openai": {
                        "sub-team-gw": {
                            "parent_provider": "openai",
                            "display_name": "Sub Team GW",
                            "sdk": "openai",
                            "billing": "cash",
                            "base_url": "https://sub-team-gw.example.com/v1",
                        }
                    }
                },
                "models": {
                    "openai": [
                        {
                            "id": "gpt-personal-only",
                            "name": "GPT Personal Only",
                            "pricing": {"input": 2.0, "output": 4.0},
                        }
                    ],
                    "sub-team-gw": [
                        {
                            "id": "team-shared-id",
                            "name": "Team Shared",
                            "pricing": {"input": 80.0, "output": 160.0},
                        }
                    ],
                },
            },
            "models": {
                "shared-alias": {
                    "model_id": "team-shared-id",
                    "provider": "sub-team-gw",
                    "display_name": "Shared Alias",
                    "visible": True,
                }
            },
            "dalton_meta": {"shared-alias": {"billing": "cash", "tool_calling": True}},
        }
        cat = compile_catalog_from_payload(payload, revision=180)
        cache_compiled_catalog(cat)
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(compiled_mod.get_compiled_catalog_sync, return_value=cat),
        )
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_async",
            create_autospec(compiled_mod.get_compiled_catalog_async, return_value=cat),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", cat.model_config)
        monkeypatch.setattr(
            tmc_db,
            "list_catalog_provider_credentials_sync",
            create_autospec(
                tmc_db.list_catalog_provider_credentials_sync,
                return_value={
                    "sub-team-gw": {
                        "provider_slug": "sub-team-gw",
                        "bound_base_url": "https://sub-team-gw.example.com/v1",
                        "canonical_bound_url": "https://sub-team-gw.example.com:443/v1",
                        "key_fingerprint": "fp-r11-team",
                    }
                },
            ),
        )
        monkeypatch.setattr(
            tmc_db,
            "list_catalog_provider_credentials",
            create_autospec(
                tmc_db.list_catalog_provider_credentials,
                return_value={
                    "sub-team-gw": {
                        "provider_slug": "sub-team-gw",
                        "bound_base_url": "https://sub-team-gw.example.com/v1",
                        "canonical_bound_url": "https://sub-team-gw.example.com:443/v1",
                        "key_fingerprint": "fp-r11-team",
                    }
                },
            ),
        )
        monkeypatch.setattr(
            ws_db,
            "lock_run_workspace",
            create_autospec(ws_db.lock_run_workspace, return_value=None),
        )

        personal_entry = {
            "name": "shared-alias",
            "provider": "openai",
            "model_id": "gpt-personal-only",
            "base_url": "https://personal.example.com/v1",
        }

        async def _fake_classify(
            uid: str,
            model_name: str,
            _pref_cache: dict[str, Any] | None = None,
            ignore_custom_models: bool = False,
        ) -> tuple[str, dict[str, Any]]:
            if not ignore_custom_models and uid == "u-personal" and model_name == "shared-alias":
                return user_models.ModelSource.CUSTOM, personal_entry
            return user_models.ModelSource.SYSTEM, {}

        monkeypatch.setattr(
            user_models,
            "classify_model",
            create_autospec(user_models.classify_model, side_effect=_fake_classify),
        )

        class _FakeCursor:
            def __init__(self) -> None:
                self._next_row: dict[str, Any] | None = None

            async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
                if "AS user_id" in sql:
                    self._next_row = {"user_id": "u-personal"}
                elif "INSERT INTO subagent_runs" in sql:
                    facts_bind = params[8]
                    facts_dict = (
                        dict(facts_bind.obj)
                        if facts_bind is not None and hasattr(facts_bind, "obj")
                        else {}
                    )
                    self._next_row = {
                        "task_run_id": params[0],
                        "thread_id": params[1],
                        "task_id": params[2],
                        "replay_facts": facts_dict,
                        "user_id": "u-personal",
                    }
                else:
                    self._next_row = None

            async def fetchone(self) -> dict[str, Any] | None:
                return self._next_row

        class _FakeConn:
            @asynccontextmanager
            async def transaction(self) -> Any:
                yield self

            @asynccontextmanager
            async def cursor(self, **_kw: Any) -> Any:
                yield _FakeCursor()

        # 1) start_task_run persists a valid empty snapshot with excluded_custom_keys
        task_run_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
        row_personal = asyncio.run(
            subagent_runs.start_task_run(
                task_run_id=task_run_id,
                thread_id="thread-r11-1",
                task_id="task-r11-personal",
                cause="init",
                actual_model_key="shared-alias",
                model={"id": "shared-alias", "display_name": "Shared Alias"},
                model_override="shared-alias",
                conn=_FakeConn(),
            )
        )
        persisted_snap = row_personal["replay_facts"]["effective_catalog_snapshot"]
        assert persisted_snap["schema"] == TURN_SNAPSHOT_SCHEMA
        assert persisted_snap["models"] == {}
        assert persisted_snap["excluded_custom_keys"] == ["shared-alias"]
        assert row_personal["replay_facts"]["catalog_revision"] == 180

        # 2) load_run_catalog_snapshot returns the empty snapshot WITHOUT calling backfill
        backfill_spy = create_autospec(tmc_db.backfill_run_catalog_snapshot, return_value=True)
        monkeypatch.setattr(tmc_db, "backfill_run_catalog_snapshot", backfill_spy)
        monkeypatch.setattr(
            snap_mod,
            "_fetch_persisted_run_row",
            create_autospec(snap_mod._fetch_persisted_run_row, return_value=row_personal),
        )
        loaded_snap = asyncio.run(load_run_catalog_snapshot(task_run_id, is_subagent=True))
        assert loaded_snap == persisted_snap
        assert backfill_spy.await_count == 0
        assert row_personal["replay_facts"]["effective_catalog_snapshot"]["models"] == {}

        # 3) Settlement under the loaded empty snapshot uses the personal model's pricing,
        #    not the team catalog rate ($80/$160), even if a parent team snapshot was active
        parent_team_snap = build_effective_catalog_snapshot(["shared-alias"], compiled=cat)
        assert parent_team_snap is not None and "shared-alias" in parent_team_snap["models"]
        with bind_catalog_snapshot(parent_team_snap):
            with bind_catalog_snapshot(loaded_snap):
                settled = calculate_cost_from_per_call_records(
                    [
                        {
                            "model_name": "shared-alias",
                            "pricing_model_id": "gpt-personal-only",
                            "pricing_provider": "openai",
                            "billing_type": "byok",
                            "usage": {
                                "input_tokens": 1_000_000,
                                "output_tokens": 1_000_000,
                            },
                        }
                    ]
                )
        # Personal pricing: $2 input + $4 output = $6, platform_cost = $0 (byok)
        assert settled["total_cost"] == pytest.approx(6.0, rel=1e-6)
        assert settled["platform_cost"] == pytest.approx(0.0, abs=1e-9)

        # 4) verify_resume_route and _handle_resume after load_run_catalog_snapshot allow resume
        asyncio.run(
            verify_resume_route(
                task_run_id,
                RouteFingerprint(
                    model_key="shared-alias",
                    provider="openai",
                    model_id="gpt-personal-only",
                    bound_base_url="https://personal.example.com:443/v1",
                    key_fingerprint=None,
                    billing="cash",
                ),
                is_subagent=True,
                run_row=row_personal,
            )
        )
        sentinel_refusal = TaskModelRefused(
            message="sentinel-r11-reached-resolve-model", reason="sentinel"
        )
        resolve_spy = create_autospec(task_actions._resolve_model, return_value=sentinel_refusal)
        monkeypatch.setattr(task_actions, "_resolve_model", resolve_spy)
        monkeypatch.setattr(
            task_actions,
            "_read_latest_run",
            create_autospec(task_actions._read_latest_run, return_value=row_personal),
        )
        fake_task = SimpleNamespace(
            task_id="task-r11-personal",
            display_id="Task-task-r11-personal",
            subagent_type="research",
            is_pending=False,
            cancelled=False,
            terminal_status=None,
            asyncio_task=None,
            result=None,
            ask=TaskAsk(model="shared-alias"),
            lock=asyncio.Lock(),
        )

        async def _resolve_ok_task(*_a: Any, **_kw: Any) -> Any:
            return fake_task

        fake_mw = SimpleNamespace(
            _resolve_or_error=_resolve_ok_task,
            subagent_types={"research"},
            _resume_claims=set(),
            registry=SimpleNamespace(
                get_by_id=lambda tid: fake_task if tid == "task-r11-personal" else None,
                run_ledger=object(),
            ),
            task_models=SimpleNamespace(
                _own_model=lambda _st: "shared-alias",
                _user_id="u-personal",
                _model_pref={"custom_models": [personal_entry]},
            ),
            namespace_owner=None,
        )
        fake_request = SimpleNamespace(
            tool_call={
                "id": "tc-r11-personal",
                "args": {"action": "resume", "task_id": "task-r11-personal"},
            },
            state={},
        )
        res = asyncio.run(
            task_actions._handle_resume(
                fake_mw,
                fake_request,
                handler=lambda _req: None,
                target_task_id="task-r11-personal",
                subagent_type=None,
                description="resume after load_run_catalog_snapshot",
                prompt="continue",
                args={"action": "resume", "task_id": "task-r11-personal"},
                parent_thread_id="thread-r11-1",
                tool_call_id="tc-r11-personal",
                current_run_id="parent-run-r11-1",
                ask=TaskAsk(),
            )
        )
        assert isinstance(res, ToolMessage)
        assert resolve_spy.call_count == 1
        assert "(sentinel)" in str(res.content)  # reached _resolve_model past the route check

        # 5) Legacy backfill strips excluded_custom_keys (both all-excluded and mixed rows)
        backfill_spy.reset_mock()
        legacy_all_excluded = {
            "task_run_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2",
            "replay_facts": {
                "actual_model_key": "shared-alias",
                "excluded_custom_keys": ["shared-alias"],
            },
        }
        snap_all_excl = asyncio.run(
            ensure_run_catalog_snapshot(legacy_all_excluded, is_subagent=True)
        )
        assert snap_all_excl is not None
        assert snap_all_excl["schema"] == TURN_SNAPSHOT_SCHEMA
        assert snap_all_excl["models"] == {}
        assert snap_all_excl["excluded_custom_keys"] == ["shared-alias"]
        assert backfill_spy.await_count == 1
        assert backfill_spy.await_args.args[1]["models"] == {}

        backfill_spy.reset_mock()
        legacy_mixed = {
            "task_run_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3",
            "replay_facts": {
                "actual_model_key": "shared-alias",
                "model_override": "gpt-test",
                "excluded_custom_keys": ["shared-alias"],
            },
        }
        snap_mixed = asyncio.run(ensure_run_catalog_snapshot(legacy_mixed, is_subagent=True))
        assert snap_mixed is not None
        assert "shared-alias" not in snap_mixed["models"]
        assert "gpt-test" in snap_mixed["models"]
        assert snap_mixed["excluded_custom_keys"] == ["shared-alias"]
        assert backfill_spy.await_count == 1

    def test_r11_2_start_task_run_classifies_outside_pool_connection_and_transaction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.llms.llm import LLM as LLMFactory
        from src.server.database import pool as pool_mod
        from src.server.database import workspace as ws_db
        from src.server.database.runs import subagent_runs
        from src.server.services.llm import user_models

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )
        cat = compile_catalog_from_payload({"schema": CATALOG_SCHEMA}, revision=181)
        cache_compiled_catalog(cat)
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(compiled_mod.get_compiled_catalog_sync, return_value=cat),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", cat.model_config)
        monkeypatch.setattr(
            ws_db,
            "lock_run_workspace",
            create_autospec(ws_db.lock_run_workspace, return_value=None),
        )

        # Single-connection pool: raises immediately on nested checkout while a connection is held.
        pool_state = {
            "active": 0,
            "max_active": 0,
            "in_tx": False,
            "classify_saw_active": None,
            "classify_saw_in_tx": None,
        }

        class _SinglePoolCursor:
            def __init__(self) -> None:
                self._next_row: dict[str, Any] | None = None

            async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
                if "AS user_id" in sql:
                    self._next_row = {"user_id": "u-pool-test"}
                elif "INSERT INTO subagent_runs" in sql:
                    facts_bind = params[8]
                    facts_dict = (
                        dict(facts_bind.obj)
                        if facts_bind is not None and hasattr(facts_bind, "obj")
                        else {}
                    )
                    self._next_row = {
                        "task_run_id": params[0],
                        "thread_id": params[1],
                        "task_id": params[2],
                        "replay_facts": facts_dict,
                        "user_id": "u-pool-test",
                    }
                else:
                    self._next_row = None

            async def fetchone(self) -> dict[str, Any] | None:
                return self._next_row

        class _SinglePoolConn:
            @asynccontextmanager
            async def transaction(self) -> Any:
                pool_state["in_tx"] = True
                try:
                    yield self
                finally:
                    pool_state["in_tx"] = False

            @asynccontextmanager
            async def cursor(self, **_kw: Any) -> Any:
                yield _SinglePoolCursor()

        @asynccontextmanager
        async def _single_slot_get_db_connection(conn: Any = None) -> Any:
            if conn is not None:
                yield conn
                return
            if pool_state["active"] >= 1:
                raise RuntimeError("pool exhausted: nested connection checkout inside start_task_run")
            pool_state["active"] += 1
            pool_state["max_active"] = max(pool_state["max_active"], pool_state["active"])
            try:
                yield _SinglePoolConn()
            finally:
                pool_state["active"] -= 1

        monkeypatch.setattr(pool_mod, "get_db_connection", _single_slot_get_db_connection)

        async def _classify_checking_out_from_same_pool(
            uid: str,
            model_name: str,
            _pref_cache: dict[str, Any] | None = None,
            ignore_custom_models: bool = False,
        ) -> tuple[str, dict[str, Any]]:
            pool_state["classify_saw_active"] = pool_state["active"]
            pool_state["classify_saw_in_tx"] = pool_state["in_tx"]
            # Simulate preference cache miss borrowing from the same 1-slot pool.
            async with pool_mod.get_db_connection():
                pass
            return (
                user_models.ModelSource.CUSTOM,
                {"name": model_name, "provider": "openai", "model_id": "gpt-personal-only"},
            )

        monkeypatch.setattr(
            user_models,
            "classify_model",
            create_autospec(
                user_models.classify_model,
                side_effect=_classify_checking_out_from_same_pool,
            ),
        )

        row = asyncio.run(
            subagent_runs.start_task_run(
                task_run_id="cccccccc-cccc-4ccc-8ccc-ccccccccccc1",
                thread_id="thread-r11-pool",
                task_id="task-r11-pool",
                cause="init",
                actual_model_key="gpt-test",
                model={"id": "gpt-test", "display_name": "GPT Test"},
                conn=None,
            )
        )
        assert pool_state["max_active"] == 1
        assert pool_state["active"] == 0
        assert pool_state["classify_saw_active"] == 0
        assert pool_state["classify_saw_in_tx"] is False
        snap = row["replay_facts"]["effective_catalog_snapshot"]
        assert snap["schema"] == TURN_SNAPSHOT_SCHEMA
        assert snap["models"] == {}
        assert snap["excluded_custom_keys"] == ["gpt-test"]


class TestR12AllowlistSourceAndPluginOverrideValidation:
    """R12 fixes: allowlisted team subagent model source forwarding and plugin update override validation."""

    def test_r12_1_allowlist_same_name_personal_model_persists_team_snapshot_and_enforces_resume(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from ptc_agent.agent.middleware.background_subagent.middleware import (
            BackgroundSubagentMiddleware,
        )
        from ptc_agent.agent.middleware.background_subagent.registry import (
            BackgroundTaskRegistry,
        )
        from ptc_agent.agent.middleware.background_subagent.subagent import (
            run_on_by_type,
        )
        from ptc_agent.config.agent import RunOn, TaskAsk, TaskModel
        from src.dalton.services.team_model_catalog import compiled as compiled_mod
        from src.dalton.services.team_model_catalog.compiled import cache_compiled_catalog
        from src.dalton.services.team_model_catalog.constants import (
            CATALOG_ROUTE_CHANGED_CODE,
        )
        from src.dalton.services.team_model_catalog.snapshot import (
            resolve_live_route_fingerprint,
            verify_resume_route,
        )
        from src.llms.llm import LLM as LLMFactory
        from src.server.database import workspace as ws_db
        from src.server.database.runs import subagent_runs
        from src.server.services import subagent_run_coordinator as coord_mod
        from src.server.services.llm import (
            availability as avail_mod,
            clients as clients_mod,
            task_model as task_model_mod,
            user_models,
        )
        from src.server.services.llm.capabilities import PlanTier
        from src.server.services.policy import Policy
        from src.server.services.subagent_run_coordinator import SubagentRunCoordinator

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        # Team catalog rev 190 defines team model "shared-alias" -> gpt-team-v1.
        cat_v1 = compile_catalog_from_payload(
            {
                "schema": CATALOG_SCHEMA,
                "models": {
                    "shared-alias": {
                        "model_id": "gpt-team-v1",
                        "provider": "openai",
                        "display_name": "Team Shared Alias",
                        "visible": True,
                        "context": 128000,
                        "input_modalities": ["text"],
                    },
                },
            },
            revision=190,
        )
        cache_compiled_catalog(cat_v1)
        real_get_compiled_sync = compiled_mod.get_compiled_catalog_sync
        real_get_compiled_async = compiled_mod.get_compiled_catalog_async
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(real_get_compiled_sync, return_value=cat_v1),
        )
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_async",
            create_autospec(real_get_compiled_async, return_value=cat_v1),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", cat_v1.model_config)
        monkeypatch.setattr(
            ws_db,
            "lock_run_workspace",
            create_autospec(ws_db.lock_run_workspace, return_value=None),
        )

        # User retains a same-name personal custom model in unfiltered DB prefs,
        # while policy models.allowed allows the team model "shared-alias".
        personal_entry = {
            "name": "shared-alias",
            "provider": "openai",
            "model_id": "gpt-personal-only",
            "display_name": "Personal Shared Alias",
        }
        raw_pref = {"custom_models": [personal_entry]}
        policy = Policy(models_allowed=frozenset({"shared-alias"}))
        filtered_pref = avail_mod.builtins_only(policy, raw_pref)
        assert "custom_models" not in filtered_pref

        # Unfiltered classify_model WOULD return CUSTOM if start_task_run called it.
        async def _unfiltered_classify(
            uid: str,
            model_name: str,
            _pref_cache: dict[str, Any] | None = None,
            ignore_custom_models: bool = False,
        ) -> tuple[str, dict[str, Any]]:
            if not ignore_custom_models and _pref_cache is None and model_name == "shared-alias":
                return user_models.ModelSource.CUSTOM, personal_entry
            if _pref_cache is not None and not ignore_custom_models:
                for cm in _pref_cache.get("custom_models") or []:
                    if isinstance(cm, dict) and cm.get("name") == model_name:
                        return user_models.ModelSource.CUSTOM, cm
            cfg = LLMFactory.get_model_config().get_model_config(model_name)
            if cfg:
                return user_models.ModelSource.SYSTEM, cfg
            return user_models.ModelSource.UNKNOWN, {}

        classify_spy = create_autospec(
            user_models.classify_model, side_effect=_unfiltered_classify
        )
        monkeypatch.setattr(user_models, "classify_model", classify_spy)

        # 1) run_on_by_type distinguishes built team client vs personal custom client vs bare stub.
        team_client = SimpleNamespace(
            metadata={"manifest_model": "shared-alias", "billing_type": "platform"}
        )
        personal_client = SimpleNamespace(
            metadata={
                "manifest_model": "shared-alias",
                "billing_type": "byok",
                "is_custom_model": True,
            }
        )
        bare_stub = SimpleNamespace()
        ro_map = run_on_by_type(
            [
                {"name": "research", "model": team_client},
                {"name": "personal-sub", "model": personal_client},
                {"name": "stub-sub", "model": bare_stub},
            ],
            default_model=team_client,
            models={},
            claude_code=None,
        )
        assert ro_map["research"].actual_model_key == "shared-alias"
        assert ro_map["research"].is_custom_model is False
        assert ro_map["research"].admission_kwargs()["is_custom_model"] is False
        assert ro_map["personal-sub"].is_custom_model is True
        assert ro_map["personal-sub"].admission_kwargs()["is_custom_model"] is True
        assert "stub-sub" not in ro_map or "is_custom_model" not in ro_map["stub-sub"].admission_kwargs()

        # 2) TaskModelGate._resolve under models.allowed + builtins_only produces TaskModel(is_custom_model=False).
        fake_cfg = SimpleNamespace(
            subsidiary_llm_clients={},
            subagents=SimpleNamespace(enabled=[], definitions={}, resolved=None),
            claude_code=False,
            claude_code_admitted=None,
            claude_code_pins=None,
            compaction=SimpleNamespace(profile="default"),
            llm_client=team_client,
        )
        monkeypatch.setattr(
            task_model_mod,
            "turn_roster",
            create_autospec(
                task_model_mod.turn_roster,
                return_value=SimpleNamespace(definitions={}),
            ),
        )
        monkeypatch.setattr(
            task_model_mod,
            "is_own_key_turn",
            create_autospec(task_model_mod.is_own_key_turn, return_value=False),
        )
        monkeypatch.setattr(
            task_model_mod,
            "plan_covers",
            create_autospec(task_model_mod.plan_covers, return_value=True),
        )
        monkeypatch.setattr(
            task_model_mod,
            "resolve_model_client",
            create_autospec(
                task_model_mod.resolve_model_client,
                return_value=clients_mod.ResolvedClient(
                    team_client,
                    user_models.ModelSource.SYSTEM,
                    clients_mod.CredentialSource.PLATFORM,
                ),
            ),
        )
        gate = task_model_mod.TaskModelGate(
            fake_cfg,
            user_id="u-allowlist",
            workspace_id="ws-1",
            thread_id="thread-r12-1",
            model_pref=filtered_pref,
            permits=PlanTier("u-allowlist"),
            is_byok=False,
            turn_model="gpt-test",
            plan_exempt=frozenset({"shared-alias"}),
            policy=policy,
        )
        resolved_tm = asyncio.run(gate.resolve(TaskAsk(model="shared-alias"), "research"))
        assert isinstance(resolved_tm, TaskModel)
        assert resolved_tm.is_custom_model is False
        assert resolved_tm.run_on.admission_kwargs()["is_custom_model"] is False

        # 3) Full admission chain: BackgroundSubagentMiddleware -> SubagentRunCoordinator -> subagent_runs.start_task_run
        captured_rows: list[dict[str, Any]] = []

        class _FakeCursor:
            def __init__(self) -> None:
                self._next_row: dict[str, Any] | None = None

            async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
                if "AS user_id" in sql:
                    self._next_row = {"user_id": "u-allowlist"}
                elif "INSERT INTO subagent_runs" in sql:
                    facts_bind = params[8]
                    facts_dict = (
                        dict(facts_bind.obj)
                        if facts_bind is not None and hasattr(facts_bind, "obj")
                        else {}
                    )
                    row = {
                        "task_run_id": params[0],
                        "thread_id": params[1],
                        "task_id": params[2],
                        "replay_facts": facts_dict,
                        "user_id": "u-allowlist",
                    }
                    captured_rows.append(row)
                    self._next_row = row
                else:
                    self._next_row = None

            async def fetchone(self) -> dict[str, Any] | None:
                return self._next_row

        class _FakeConn:
            @asynccontextmanager
            async def transaction(self) -> Any:
                yield self

            @asynccontextmanager
            async def cursor(self, **_kw: Any) -> Any:
                yield _FakeCursor()

        from src.server.database import pool as pool_mod
        from src.server.services import thread_control_stream as tcs_mod
        from src.utils.cache import redis_cache as redis_cache_mod

        @asynccontextmanager
        async def _fake_pool_conn(conn: Any = None) -> Any:
            yield conn if conn is not None else _FakeConn()

        monkeypatch.setattr(pool_mod, "get_db_connection", _fake_pool_conn)
        monkeypatch.setattr(
            redis_cache_mod,
            "get_cache_client",
            create_autospec(
                redis_cache_mod.get_cache_client,
                return_value=SimpleNamespace(enabled=False),
            ),
        )
        monkeypatch.setattr(
            tcs_mod,
            "announce_task_run_started",
            create_autospec(tcs_mod.announce_task_run_started, return_value=None),
        )
        monkeypatch.setattr(
            coord_mod,
            "sr_db",
            subagent_runs,
        )

        classify_spy.reset_mock()
        coord = SubagentRunCoordinator("thread-r12-1")
        registry = BackgroundTaskRegistry(thread_id="thread-r12-1")
        registry.run_ledger = coord
        mw = BackgroundSubagentMiddleware(
            subagent_types={"research", "fallback-type"},
            registry=registry,
            run_on={
                "research": ro_map["research"],
                # Omit is_custom_model on RunOn to verify middleware model_client_for fallback.
                "fallback-type": RunOn(actual_model_key="shared-alias"),
            },
            default_model_client=team_client,
        )
        task_research = asyncio.run(
            registry.register(
                tool_call_id="tc-r12-1",
                description="allowlist team run",
                prompt="analyze",
                subagent_type="research",
                run_id="parent-run-r12-1",
            )
        )
        run_id_team = asyncio.run(
            mw.admit_task_run(
                task_research,
                cause="init",
                description="allowlist team run",
                launch_tool_call_id="tc-r12-1",
                parent_run_id="parent-run-r12-1",
            )
        )
        assert run_id_team
        assert classify_spy.await_count == 0
        row_team = captured_rows[-1]
        snap_team = row_team["replay_facts"]["effective_catalog_snapshot"]
        assert snap_team["schema"] == TURN_SNAPSHOT_SCHEMA
        assert "shared-alias" in snap_team["models"]
        assert snap_team["models"]["shared-alias"]["model_id"] == "gpt-team-v1"
        assert "excluded_custom_keys" not in snap_team

        # Verify middleware model_client_for fallback also forwards is_custom_model=False.
        task_fb = asyncio.run(
            registry.register(
                tool_call_id="tc-r12-2",
                description="fallback client source run",
                prompt="analyze",
                subagent_type="fallback-type",
                run_id="parent-run-r12-1",
            )
        )
        asyncio.run(
            mw.admit_task_run(
                task_fb,
                cause="init",
                description="fallback client source run",
                launch_tool_call_id="tc-r12-2",
                parent_run_id="parent-run-r12-1",
            )
        )
        assert classify_spy.await_count == 0
        snap_fb = captured_rows[-1]["replay_facts"]["effective_catalog_snapshot"]
        assert snap_fb["models"]["shared-alias"]["model_id"] == "gpt-team-v1"
        assert "excluded_custom_keys" not in snap_fb

        # 4) Explicit is_custom_model=True skips classify_model and persists empty excluded snapshot.
        row_custom = asyncio.run(
            subagent_runs.start_task_run(
                task_run_id="dddddddd-dddd-4ddd-8ddd-ddddddddddd1",
                thread_id="thread-r12-1",
                task_id="task-r12-personal",
                cause="init",
                actual_model_key="shared-alias",
                is_custom_model=True,
                conn=_FakeConn(),
            )
        )
        assert classify_spy.await_count == 0
        snap_custom = row_custom["replay_facts"]["effective_catalog_snapshot"]
        assert snap_custom["models"] == {}
        assert snap_custom["excluded_custom_keys"] == ["shared-alias"]

        # 5) Resume verification on the allowlisted team run enforces team route continuity:
        #    unchanged route passes; changed team route (gpt-team-v2) raises 400 catalog_route_changed.
        live_same = asyncio.run(
            resolve_live_route_fingerprint(
                "shared-alias",
                user_id="u-allowlist",
                model_pref=filtered_pref,
            )
        )
        assert live_same.model_id == "gpt-team-v1"
        asyncio.run(
            verify_resume_route(
                row_team["task_run_id"],
                live_same,
                is_subagent=True,
                run_row=row_team,
            )
        )

        cat_v2 = compile_catalog_from_payload(
            {
                "schema": CATALOG_SCHEMA,
                "models": {
                    "shared-alias": {
                        "model_id": "gpt-team-v2",
                        "provider": "openai",
                        "display_name": "Team Shared Alias v2",
                        "visible": True,
                        "context": 128000,
                        "input_modalities": ["text"],
                    },
                },
            },
            revision=191,
        )
        cache_compiled_catalog(cat_v2)
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_sync",
            create_autospec(real_get_compiled_sync, return_value=cat_v2),
        )
        monkeypatch.setattr(
            compiled_mod,
            "get_compiled_catalog_async",
            create_autospec(real_get_compiled_async, return_value=cat_v2),
        )
        monkeypatch.setattr(LLMFactory, "_model_config", cat_v2.model_config)
        live_changed = asyncio.run(
            resolve_live_route_fingerprint(
                "shared-alias",
                user_id="u-allowlist",
                model_pref=filtered_pref,
            )
        )
        assert live_changed.model_id == "gpt-team-v2"
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                verify_resume_route(
                    row_team["task_run_id"],
                    live_changed,
                    is_subagent=True,
                    run_row=row_team,
                )
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == CATALOG_ROUTE_CHANGED_CODE

    def test_r12_2_plugin_update_validates_active_user_override_when_model_omitted_or_inherit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.services.team_model_catalog import (
            compiled as compiled_mod,
            service as tmc_service,
        )
        from src.server.services.llm import user_models
        from src.server.services.plugins import subagent_fanout
        from src.server.services.plugins.agents import read_agent

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.compiled._load_json_dict",
            lambda p: _base_upstream_models()
            if "models.json" in str(p)
            else (_base_upstream_providers() if "providers.json" in str(p) else {}),
        )
        monkeypatch.setattr(
            "src.dalton.llm.overlay.load_overlay_data",
            lambda path=None: _base_native_overlay(),
        )

        cat_retired = compile_catalog_from_payload(
            {
                "schema": CATALOG_SCHEMA,
                "retired": [
                    {
                        "key": "retired-model",
                        "retired_at": "2026-11-25T00:00:00Z",
                        "reason": "deprecated",
                    }
                ],
            },
            revision=192,
            from_db=True,
        )
        cat_no_db = compile_catalog_from_payload(
            {"schema": CATALOG_SCHEMA},
            revision=0,
            from_db=False,
        )

        plan_omitted = read_agent(
            "screener",
            b"---\ndescription: Screens tickers.\n---\nScreen them.\n",
        )
        plan_inherit = read_agent(
            "screener",
            b"---\ndescription: Screens tickers.\nmodel: inherit\n---\nScreen them.\n",
        )
        plan_declares_retired = read_agent(
            "screener",
            b"---\ndescription: Screens tickers.\nmodel: retired-model\n---\nScreen them.\n",
        )
        assert "model" not in plan_omitted.fields
        assert "model" not in plan_inherit.fields
        assert plan_declares_retired.fields["model"] == "retired-model"

        fake_conn = object()
        monkeypatch.setattr(
            user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value={}),
        )

        # Save references to the real functions before monkeypatching.
        real_get_compiled = compiled_mod.get_compiled_catalog_async
        real_list_user_subagents = subagent_fanout.list_user_subagents

        # 1) Update (check_overrides=True): incoming omits model or writes model: inherit,
        #    while user's still-active override points to a retired team model -> must reject.
        compiled_spy = create_autospec(real_get_compiled, return_value=cat_retired)
        monkeypatch.setattr(compiled_mod, "get_compiled_catalog_async", compiled_spy)
        monkeypatch.setattr(tmc_service, "get_compiled_catalog_async", compiled_spy)
        monkeypatch.setattr(
            subagent_fanout,
            "list_user_subagents",
            create_autospec(
                real_list_user_subagents,
                return_value=[
                    {
                        "kind": "override",
                        "base_key": "plugin:demo/screener",
                        "fields": {"model": "retired-model"},
                    }
                ],
            ),
        )
        for plan in (plan_omitted, plan_inherit):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(
                    subagent_fanout._validate_plugin_subagent_models_under_lock(
                        "u-1", "demo", [plan], conn=fake_conn, check_overrides=True
                    )
                )
            assert exc_info.value.status_code == 400
            assert exc_info.value.detail["code"] == "model_retired"

        # 2) Update where plugin declares retired-model, but user override selects an active model
        #    or explicitly inherits (None) -> effective model is allowed.
        for override_fields in ({"model": "gpt-test"}, {"model": None}):
            monkeypatch.setattr(
                subagent_fanout,
                "list_user_subagents",
                create_autospec(
                    real_list_user_subagents,
                    return_value=[
                        {
                            "kind": "override",
                            "base_key": "plugin:demo/screener",
                            "fields": override_fields,
                        }
                    ],
                ),
            )
            asyncio.run(
                subagent_fanout._validate_plugin_subagent_models_under_lock(
                    "u-1", "demo", [plan_declares_retired], conn=fake_conn, check_overrides=True
                )
            )

        # 3) Fresh install (check_overrides=False): undeclared / inherit plans skip catalog &
        #    override queries completely; declared retired model still rejects without override read.
        compiled_spy_install = create_autospec(real_get_compiled, return_value=cat_retired)
        list_spy_install = create_autospec(real_list_user_subagents, return_value=[])
        monkeypatch.setattr(compiled_mod, "get_compiled_catalog_async", compiled_spy_install)
        monkeypatch.setattr(tmc_service, "get_compiled_catalog_async", compiled_spy_install)
        monkeypatch.setattr(subagent_fanout, "list_user_subagents", list_spy_install)
        asyncio.run(
            subagent_fanout._validate_plugin_subagent_models_under_lock(
                "u-1", "demo", [plan_omitted, plan_inherit], conn=fake_conn, check_overrides=False
            )
        )
        assert compiled_spy_install.await_count == 0
        assert list_spy_install.await_count == 0

        with pytest.raises(HTTPException) as exc_install:
            asyncio.run(
                subagent_fanout._validate_plugin_subagent_models_under_lock(
                    "u-1", "demo", [plan_declares_retired], conn=fake_conn, check_overrides=False
                )
            )
        assert exc_install.value.status_code == 400
        assert exc_install.value.detail["code"] == "model_retired"
        assert list_spy_install.await_count == 0

        # 4) Update when no team catalog is configured (from_db=False): returns early with no
        #    list_user_subagents query (keeps test_plugin_subagent_lifecycle.py free of extra SQL).
        compiled_spy_nodb = create_autospec(real_get_compiled, return_value=cat_no_db)
        list_spy_nodb = create_autospec(real_list_user_subagents, return_value=[])
        monkeypatch.setattr(compiled_mod, "get_compiled_catalog_async", compiled_spy_nodb)
        monkeypatch.setattr(tmc_service, "get_compiled_catalog_async", compiled_spy_nodb)
        monkeypatch.setattr(subagent_fanout, "list_user_subagents", list_spy_nodb)
        asyncio.run(
            subagent_fanout._validate_plugin_subagent_models_under_lock(
                "u-1", "demo", [plan_omitted, plan_inherit], conn=fake_conn, check_overrides=True
            )
        )
        assert compiled_spy_nodb.await_count == 1
        assert list_spy_nodb.await_count == 0
