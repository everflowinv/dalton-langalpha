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

    def test_r2_2_snapshot_authoritative_server_binding_across_resume_retry_and_settlement(
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
            catalog_snapshot={"schema": "dalton-turn-catalog-snapshot/1", "models": {}},
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
            catalog_snapshot={"schema": "dalton-turn-catalog-snapshot/1", "models": {}},
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

        # 5. SubagentRunCoordinator resume inherits predecessor replay_facts['effective_catalog_snapshot']
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
        assert recorded_sub_kwargs["catalog_snapshot"] == snap_v1

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

    def test_r4_2_resumed_turn_uses_snapshot_route_params_fingerprint_and_pricing_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.database import team_model_catalog as tmc_db
        from src.dalton.services.team_model_catalog.snapshot import lookup_snapshot_pricing
        from src.dalton.services.team_model_catalog.transport import (
            resolve_catalog_variant_binding_async,
        )
        from src.llms.llm import LLM, create_llm
        from src.server.services.llm import user_models

        # Revision 1: team-m1 routes to variant-a (model_id="m-a", temperature=0.2, input=$2)
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

        # Revision 2 (live catalog after interruption): team-m1 moved to variant-b (model_id="m-b", temp=0.9)
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

        # 1. Under bound snapshot, classify_model & LLM construction keep snapshot route (variant-a, m-a, temp=0.2)
        with bind_catalog_snapshot(snap_a):
            source, cfg = asyncio.run(
                user_models.classify_model("u-1", "team-m1", _pref_cache={})
            )
            assert source == user_models.ModelSource.SYSTEM
            assert cfg["provider"] == "variant-a"
            assert cfg["model_id"] == "m-a"
            assert cfg["parameters"]["temperature"] == 0.2

            # Credential binding resolves variant-a (not variant-b) and checks fp_a!
            factory_a = make_catalog_variant_transport_factory(
                provider_slug="variant-a",
                bound_base_url="https://a.example.com/v1",
                expected_key_fingerprint=fp_a,
                verify_db_binding=False,
            )
            llm_obj = LLM(
                "team-m1",
                api_key="sk-variant-a-1111",
                base_url_override="https://a.example.com/v1",
                egress_transport=factory_a,
                follow_redirects=False,
                _catalog_credential_source=catalog_variant_source_tag("variant-a"),
            )
            assert llm_obj.provider == "variant-a"
            assert llm_obj.model == "m-a"
            assert llm_obj.parameters["temperature"] == 0.2
            assert llm_obj.base_url == "https://a.example.com/v1"

            # Rotating variant-a's key causes create_llm("team-m1") to revoke against variant-a's snapshot fp
            rotated_fp_a = make_key_fingerprint("sk-variant-a-rotated-9999")
            monkeypatch.setattr(
                tmc_db,
                "get_catalog_provider_credential_sync",
                create_autospec(
                    tmc_db.get_catalog_provider_credential_sync,
                    side_effect=lambda slug, decrypt=True: (
                        {
                            "provider_slug": "variant-a",
                            "api_key": "sk-variant-a-rotated-9999",
                            "bound_base_url": "https://a.example.com/v1",
                            "canonical_bound_url": "https://a.example.com:443/v1",
                            "key_fingerprint": rotated_fp_a,
                        }
                        if slug == "variant-a"
                        else {
                            "provider_slug": "variant-b",
                            "api_key": "sk-variant-b-2222",
                            "bound_base_url": "https://b.example.com/v1",
                            "canonical_bound_url": "https://b.example.com:443/v1",
                            "key_fingerprint": make_key_fingerprint("sk-variant-b-2222"),
                        }
                    ),
                ),
            )
            with pytest.raises(CatalogVariantCredentialRevokedError) as rev_exc:
                create_llm("team-m1")
            assert "variant-a" in str(rev_exc.value)

            # Direct call attempting to bind variant-b for model_key="team-m1" under snap_a is rejected
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
