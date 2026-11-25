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
)
from src.dalton.services.team_model_catalog.constants import (
    CATALOG_SCHEMA,
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

        mock_inner.handle_async_request = AsyncMock(side_effect=_fake_handle_async)

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

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.get_db_connection", _fake_get_db
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.acquire_catalog_exclusive_lock",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.get_catalog_row",
            AsyncMock(
                return_value={
                    "revision": 2,
                    "payload": payload_c2,
                    "payload_sha256": "sha2",
                    "legacy_sync_import_status": "not_applicable",
                    "last_known_legacy_retired": [],
                }
            ),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.get_catalog_revision",
            AsyncMock(return_value={"revision": 1, "payload": payload_t1}),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.list_catalog_provider_credentials",
            AsyncMock(
                return_value={
                    "corp-gw": {
                        "provider_slug": "corp-gw",
                        "bound_base_url": "https://corp.example.com/v2",
                        "canonical_bound_url": "https://corp.example.com:443/v2",
                        "key_fingerprint": "sk-...2222",
                    }
                }
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

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.get_db_connection", _fake_get_db
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.acquire_catalog_exclusive_lock",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.get_catalog_row",
            AsyncMock(
                return_value={
                    "revision": 1,
                    "payload": {"schema": CATALOG_SCHEMA, "retired": [{"key": "model-a"}]},
                    "payload_sha256": "sha1",
                    "legacy_sync_import_status": "failed",
                    "last_known_legacy_retired": ["model-a"],
                }
            ),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.extract_disk_legacy_retired_readonly",
            lambda legacy_path=None: set(),
        )
        monkeypatch.setattr(
            "src.dalton.database.team_model_catalog.fetch_legacy_retired_history",
            AsyncMock(return_value=set()),
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

        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.get_db_connection", _fake_get_db
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.acquire_catalog_exclusive_lock",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.publish_catalog_changed",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.invalidate_catalog_redis_caches",
            AsyncMock(),
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
            AsyncMock(
                return_value={
                    "revision": 0,
                    "payload": {"schema": CATALOG_SCHEMA},
                    "payload_sha256": "sha0",
                    "legacy_sync_import_status": "pending",
                    "legacy_sync_imported_at": None,
                    "last_known_legacy_retired": [],
                }
            ),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.save_catalog_revision",
            AsyncMock(
                return_value={
                    "catalog": {"revision": 1, "legacy_sync_import_status": "imported"},
                    "revision_record": {"revision": 1},
                }
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
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.legacy_import.fetch_legacy_retired_history",
            AsyncMock(return_value={"model-a"}),
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
        monkeypatch.setattr(
            "src.dalton.services.team_model_catalog.service.get_compiled_catalog_async",
            AsyncMock(return_value=cat_retired),
        )
        with pytest.raises(HTTPException) as ret_exc:
            asyncio.run(assert_model_not_retired_under_lock(_FakeConn(), "model-b"))
        assert ret_exc.value.status_code == 400
        assert ret_exc.value.detail["code"] == "model_retired"
