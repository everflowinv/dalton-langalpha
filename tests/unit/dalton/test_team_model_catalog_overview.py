"""Unit tests for GET /api/v1/dalton/team-models overview edit-echo & metadata fields."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.services.team_model_catalog import compiled as compiled_mod
from src.dalton.services.team_model_catalog import follow as follow_mod
from src.dalton.services.team_model_catalog import service as service_mod
from src.dalton.services.team_model_catalog.compiled import (
    compile_catalog_from_payload,
    reset_catalog_caches_for_tests,
)
from src.dalton.services.team_model_catalog.constants import (
    CATALOG_SCHEMA,
    CatalogConflictError,
    CatalogValidationError,
)
from src.dalton.services.team_model_catalog.validation import (
    _SUPPORTED_VARIANT_SDKS,
    CatalogModelUpsertInput,
)


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
                "accessToken": "camel-access-token-leak",
                "aws_session_token": "aws-sess-token-leak",
                "x-custom-auth": "hyphen-auth-leak",
                "secret-token": "hyphen-secret-leak",
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


_ORIG_GET_COMPILED_CATALOG_ASYNC = compiled_mod.get_compiled_catalog_async
_ORIG_LIST_CATALOG_PROVIDER_CREDENTIALS = service_mod.list_catalog_provider_credentials
_ORIG_GET_CATALOG_ROW = service_mod.get_catalog_row


def _patch_overview_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    compiled: Any,
    *,
    creds: dict[str, dict[str, Any]] | None = None,
) -> None:
    get_compiled_mock = create_autospec(_ORIG_GET_COMPILED_CATALOG_ASYNC, return_value=compiled)
    list_creds_mock = create_autospec(
        _ORIG_LIST_CATALOG_PROVIDER_CREDENTIALS,
        return_value=creds or {},
    )
    get_row_mock = create_autospec(
        _ORIG_GET_CATALOG_ROW,
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
    }

    acme_fast = by_key["acme-fast"]
    assert acme_fast["prompt_guidance"] == "lean"
    assert acme_fast["alias"] == ["acme-fast-alias"]
    assert acme_fast["parameters"] == {"max_output_tokens": 8192}
    assert acme_fast["task_costs_bound"] == {"model": "unmeasured-ref", "times": 1.25}

    serialized = str(overview)
    for forbidden in (
        "sk-should-never-leak",
        "camel-access-token-leak",
        "aws-sess-token-leak",
        "hyphen-auth-leak",
        "hyphen-secret-leak",
        "accessToken",
        "aws_session_token",
        "x-custom-auth",
        "secret-token",
        "secret-header",
        "top-secret",
        "tok-123",
        "top_p",
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


class _RecordingTxConn:
    """Minimal transactional DB connection stub that records commit/rollback state."""

    def __init__(self) -> None:
        self.tx_started = 0
        self.committed = 0
        self.rolled_back = 0

    @asynccontextmanager
    async def transaction(self):
        self.tx_started += 1
        try:
            yield self
        except BaseException:
            self.rolled_back += 1
            raise
        else:
            self.committed += 1


def _patch_tx_catalog_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial_payload: dict[str, Any],
    initial_revision: int = 3,
    existing_creds: dict[str, dict[str, Any]] | None = None,
    upstream_models: dict[str, Any] | None = None,
    upstream_providers: dict[str, Any] | None = None,
    native_overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conn = _RecordingTxConn()
    state: dict[str, Any] = {
        "conn": conn,
        "row": {
            "revision": initial_revision,
            "payload": initial_payload,
            "legacy_sync_import_status": "not_applicable",
            "last_known_legacy_retired": [],
        },
        "creds": dict(existing_creds or {}),
        "upserted_creds": [],
        "saved_calls": [],
    }
    u_models = upstream_models if upstream_models is not None else _sample_upstream_models()
    u_providers = upstream_providers if upstream_providers is not None else _sample_upstream_providers()
    n_overlay = native_overlay if native_overlay is not None else {}

    @asynccontextmanager
    async def _fake_get_db_connection(existing=None):
        yield existing or conn

    orig_compile = compiled_mod.compile_catalog_from_payload

    def _compile_with_sample(payload, **kwargs):
        kwargs.setdefault("oauth_sync_enabled", True)
        kwargs.setdefault("native_overlay_override", n_overlay)
        kwargs.setdefault("upstream_models_override", u_models)
        kwargs.setdefault("upstream_providers_override", u_providers)
        return orig_compile(payload, **kwargs)

    async def _fake_lock(_conn):
        return None

    async def _fake_get_row(*, conn=None, for_update=False):
        return state["row"]

    async def _fake_list_creds(*, conn=None):
        return state["creds"]

    async def _fake_upsert_cred(
        _conn, *, provider_slug, api_key, bound_base_url, updated_by
    ):
        rec = {
            "provider_slug": provider_slug,
            "api_key": api_key,
            "bound_base_url": bound_base_url,
            "canonical_bound_url": bound_base_url,
            "key_fingerprint": f"fp-{provider_slug}",
            "updated_by": updated_by,
        }
        state["upserted_creds"].append(rec)
        return rec

    async def _fake_save_rev(
        _conn,
        *,
        expected_revision,
        action,
        target_keys,
        payload,
        diff_summary,
        side_effects,
        actor_user_id,
        comment=None,
        restored_from=None,
    ):
        next_rev = int(expected_revision) + 1
        rec = {
            "revision": next_rev,
            "action": action,
            "target_keys": list(target_keys),
            "payload": payload,
            "diff_summary": diff_summary,
            "side_effects": side_effects,
            "actor_user_id": actor_user_id,
            "comment": comment,
            "restored_from": restored_from,
        }
        state["saved_calls"].append(rec)
        state["row"] = {**state["row"], "revision": next_rev, "payload": payload}
        return {"catalog": {"revision": next_rev}, "revision_record": rec}

    async def _fake_preflight(_url: str):
        return None

    async def _fake_publish(_rev: int):
        return None

    async def _fake_invalidate(*, actor_user_id=None, compiled=None):
        return None

    monkeypatch.setattr(service_mod, "get_db_connection", _fake_get_db_connection)
    monkeypatch.setattr(service_mod, "acquire_catalog_exclusive_lock", _fake_lock)
    monkeypatch.setattr(service_mod, "get_catalog_row", _fake_get_row)
    monkeypatch.setattr(service_mod, "list_catalog_provider_credentials", _fake_list_creds)
    monkeypatch.setattr(service_mod, "upsert_catalog_provider_credential", _fake_upsert_cred)
    monkeypatch.setattr(service_mod, "save_catalog_revision", _fake_save_rev)
    monkeypatch.setattr(service_mod, "preflight_catalog_variant_url", _fake_preflight)
    monkeypatch.setattr(service_mod, "publish_catalog_changed", _fake_publish)
    monkeypatch.setattr(service_mod, "invalidate_catalog_redis_caches", _fake_invalidate)
    monkeypatch.setattr(service_mod, "compile_catalog_from_payload", _compile_with_sample)
    monkeypatch.setattr(compiled_mod, "compile_catalog_from_payload", _compile_with_sample)
    return state


def test_update_model_preserves_hidden_parameters_and_deletes_explicit_null_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_payload: dict[str, Any] = {
        "schema": CATALOG_SCHEMA,
        "models": {
            "acme-fast": {
                "model_id": "acme-fast-v1",
                "provider": "openai",
                "display_name": "Acme Fast",
                "visible": True,
                "prompt_guidance": "detailed",
                "compaction_profile": "spacious",
                "context": 128000,
                "parameters": {"max_tokens": 8192, "top_p": 0.9, "temperature": 0.2},
                "reasoning": {
                    "efforts": ["low", "medium", "high"],
                    "default": "medium",
                    "write": "parameters.reasoning.effort",
                },
            }
        },
        "providers": {
            "models": {
                "openai": [
                    {
                        "id": "acme-fast-v1",
                        "name": "Acme Fast",
                        "pricing": {"input": 1.0, "output": 4.0},
                    }
                ]
            }
        },
        "dalton_meta": {
            "acme-fast": {
                "family": "acme",
                "notional_pricing": {"input": 1.0, "output": 4.0},
                "tool_calling": True,
            }
        },
        "task_costs": {
            "bounds": {
                "acme-fast": {"model": "gpt-5", "times": 1.2},
            }
        },
    }
    state = _patch_tx_catalog_env(monkeypatch, initial_payload=initial_payload, initial_revision=4)

    # Clear prompt_guidance, compaction_profile, max_tokens, reasoning, notional_pricing, task_costs_bound
    parsed = CatalogModelUpsertInput.model_validate(
        {
            "prompt_guidance": None,
            "compaction_profile": None,
            "parameters": {"max_tokens": None, "max_output_tokens": None},
            "reasoning": None,
            "dalton_meta": {"notional_pricing": None},
            "task_costs_bound": None,
            "expected_revision": 4,
        }
    )
    res = asyncio.run(
        service_mod.update_catalog_model(
            actor_user_id="admin-1", model_key="acme-fast", data=parsed
        )
    )
    assert res["revision"] == 5
    saved_payload = state["saved_calls"][0]["payload"]
    saved_model = saved_payload["models"]["acme-fast"]
    assert "prompt_guidance" not in saved_model
    assert "compaction_profile" not in saved_model
    assert "reasoning" not in saved_model
    # Hidden non-UI parameters (top_p, temperature) stay preserved server-side
    assert saved_model["parameters"] == {"top_p": 0.9, "temperature": 0.2}

    compiled = compiled_mod.compile_catalog_from_payload(saved_payload, revision=5)
    entry = compiled.model_config.get_model_config("acme-fast")
    assert entry is not None
    assert "prompt_guidance" not in entry
    assert "compaction_profile" not in entry
    assert "reasoning" not in entry
    assert entry["parameters"] == {"top_p": 0.9, "temperature": 0.2}
    assert "notional_pricing" not in (compiled.dalton_meta.get("acme-fast") or {})
    assert compiled.dalton_meta["acme-fast"]["tool_calling"] is True
    assert "acme-fast" not in (compiled.task_costs.bounds if compiled.task_costs else {})


def test_atomic_model_save_rolls_back_variant_and_curated_on_mid_transaction_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_payload: dict[str, Any] = {
        "schema": CATALOG_SCHEMA,
        "providers": {
            "provider_variants": {
                "openai": {
                    "acme-gw": {
                        "display_name": "Acme Gateway",
                        "sdk": "openai",
                        "parent_provider": "openai",
                        "access_type": "api_key",
                        "billing": "cash",
                        "base_url": "https://gw-v1.acme.example.com/v1",
                    }
                }
            }
        },
        "curated_setup": ["gpt-5"],
    }
    state = _patch_tx_catalog_env(
        monkeypatch,
        initial_payload=initial_payload,
        initial_revision=6,
        existing_creds={
            "acme-gw": {
                "provider_slug": "acme-gw",
                "bound_base_url": "https://gw-v1.acme.example.com/v1",
                "canonical_bound_url": "https://gw-v1.acme.example.com/v1",
                "key_fingerprint": "fp-old",
            }
        },
    )

    # Variant update is valid, but model has invalid max_tokens > context (fails mid-transaction!)
    failing_input = CatalogModelUpsertInput.model_validate(
        {
            "model_key": "acme-bad-model",
            "model_id": "acme-bad-v1",
            "provider": "acme-gw",
            "display_name": "Acme Bad",
            "context": 32000,
            "parameters": {"max_tokens": 64000},
            "pricing": {"input": 1.0, "output": 4.0},
            "provider_variant_update": {
                "provider_slug": "acme-gw",
                "display_name": "Acme Gateway",
                "sdk": "openai",
                "parent_provider": "openai",
                "access_type": "api_key",
                "billing": "cash",
                "base_url": "https://gw-v2.acme.example.com/v1",
                "api_key": "sk-new-v2-key",
            },
            "curated_setup_member": True,
            "expected_revision": 6,
        }
    )

    with pytest.raises(CatalogValidationError) as exc_info:
        asyncio.run(
            service_mod.create_catalog_model(actor_user_id="admin-1", data=failing_input)
        )
    assert exc_info.value.code == "invalid_max_tokens"
    assert state["conn"].tx_started == 1
    assert state["conn"].rolled_back == 1
    assert state["conn"].committed == 0
    assert state["saved_calls"] == []
    assert state["row"]["revision"] == 6

    # Now fix max_tokens and verify all 3 changes commit atomically in ONE revision bump
    valid_input = CatalogModelUpsertInput.model_validate(
        {
            "model_key": "acme-good-model",
            "model_id": "acme-good-v1",
            "provider": "acme-gw",
            "display_name": "Acme Good",
            "context": 128000,
            "parameters": {"max_tokens": 16384},
            "pricing": {"input": 1.0, "output": 4.0},
            "provider_variant_update": {
                "provider_slug": "acme-gw",
                "display_name": "Acme Gateway",
                "sdk": "openai",
                "parent_provider": "openai",
                "access_type": "api_key",
                "billing": "cash",
                "base_url": "https://gw-v2.acme.example.com/v1",
                "api_key": "sk-new-v2-key",
            },
            "curated_setup_member": True,
            "expected_revision": 6,
        }
    )
    ok_res = asyncio.run(
        service_mod.create_catalog_model(actor_user_id="admin-1", data=valid_input)
    )
    assert ok_res["revision"] == 7
    assert state["conn"].committed == 1
    assert len(state["saved_calls"]) == 1
    committed_payload = state["saved_calls"][0]["payload"]
    assert committed_payload["providers"]["provider_variants"]["openai"]["acme-gw"]["base_url"] == (
        "https://gw-v2.acme.example.com/v1"
    )
    assert "acme-good-model" in committed_payload["models"]
    assert committed_payload["curated_setup"] == ["gpt-5", "acme-good-model"]


def test_list_revisions_view_prunes_oauth_fields_when_native_oauth_sync_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = compile_catalog_from_payload(
        {"schema": CATALOG_SCHEMA},
        revision=10,
        oauth_sync_enabled=False,
        native_overlay_override={},
        upstream_models_override=_sample_upstream_models(),
        upstream_providers_override=_sample_upstream_providers(),
    )
    get_compiled_mock = create_autospec(
        compiled_mod.get_compiled_catalog_async, return_value=compiled
    )
    list_revs_mock = create_autospec(
        service_mod.list_catalog_revisions,
        return_value=[
            {
                "revision": 10,
                "action": "update_model",
                "target_keys": ["gpt-5", "gemini-4-neon-high-oauth"],
                "payload_sha256": "sha10",
                "diff_summary": {
                    "updated": "gemini-4-neon-high-oauth",
                    "models": ["gpt-5", "claude-test-oauth"],
                    "provider": "openai",
                },
                "side_effects": [
                    {
                        "kind": "automation",
                        "id": "a1",
                        "before": {"llm_model": "gemini-4-neon-high-oauth"},
                        "after": {"llm_model": "gpt-5"},
                    },
                    {
                        "kind": "automation",
                        "id": "a2",
                        "before": {"llm_model": "gpt-4o"},
                        "after": {"llm_model": "gpt-5"},
                    },
                ],
                "comment": "History entry",
                "restored_from": None,
                "actor_user_id": "admin-1",
                "actor_label": "Admin",
                "created_at": "2026-11-25T10:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(service_mod, "get_compiled_catalog_async", get_compiled_mock)
    monkeypatch.setattr(service_mod, "list_catalog_revisions", list_revs_mock)

    view = asyncio.run(service_mod.list_revisions_view())
    assert view["revision"] == 10
    assert len(view["revisions"]) == 1
    rev = view["revisions"][0]
    assert rev["revision"] == 10
    assert rev["target_keys"] == ["gpt-5"]
    assert rev["diff_summary"] == {"models": ["gpt-5"], "provider": "openai"}
    assert len(rev["side_effects"]) == 1
    assert rev["side_effects"][0]["id"] == "a2"
    assert "-oauth" not in str(rev)


def test_catalog_revision_header_middleware_emits_only_when_dalton_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton import settings as dalton_settings

    compiled = compile_catalog_from_payload(
        {"schema": CATALOG_SCHEMA},
        revision=15,
        oauth_sync_enabled=False,
        native_overlay_override={},
        upstream_models_override=_sample_upstream_models(),
        upstream_providers_override=_sample_upstream_providers(),
    )
    get_compiled_mock = create_autospec(
        compiled_mod.get_compiled_catalog_async, return_value=compiled
    )
    monkeypatch.setattr(compiled_mod, "get_compiled_catalog_async", get_compiled_mock)

    async def _dummy_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = follow_mod.CatalogRevisionHeaderMiddleware(_dummy_app)

    async def _invoke(path: str) -> list[tuple[bytes, bytes]]:
        captured: list[dict[str, Any]] = []

        async def _receive():
            return {"type": "http.request", "body": b""}

        async def _send(msg):
            captured.append(msg)

        await mw({"type": "http", "path": path}, _receive, _send)
        start = next(m for m in captured if m["type"] == "http.response.start")
        return list(start["headers"])

    monkeypatch.setattr(dalton_settings, "is_dalton_enabled", lambda: True)
    pref_headers = dict(asyncio.run(_invoke("/api/v1/users/me/preferences")))
    chat_headers = dict(asyncio.run(_invoke("/api/v1/threads/th-1/messages")))
    stream_headers = dict(asyncio.run(_invoke("/api/v1/threads/th-1/messages/stream")))
    retry_headers = dict(asyncio.run(_invoke("/api/v1/threads/th-1/retry")))
    assert pref_headers.get(b"x-dalton-catalog-revision") == b"15"
    assert chat_headers.get(b"x-dalton-catalog-revision") == b"15"
    assert stream_headers.get(b"x-dalton-catalog-revision") == b"15"
    assert retry_headers.get(b"x-dalton-catalog-revision") == b"15"

    monkeypatch.setattr(dalton_settings, "is_dalton_enabled", lambda: False)
    disabled_headers = dict(asyncio.run(_invoke("/api/v1/users/me/preferences")))
    disabled_retry_headers = dict(asyncio.run(_invoke("/api/v1/threads/th-1/retry")))
    assert b"x-dalton-catalog-revision" not in disabled_headers
    assert b"x-dalton-catalog-revision" not in disabled_retry_headers


def test_revision_detail_omits_historical_payload_and_secrets_while_preserving_ui_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import Response

    from src.dalton.app import router as dalton_router
    from src.dalton.database import team_model_catalog as db_catalog

    historical_row = {
        "revision": 4,
        "action": "update_model",
        "target_keys": ["gpt-5", "claude-test-oauth"],
        "payload_sha256": "sha-4",
        "diff_summary": {"updated": "gpt-5", "fields": ["display_name"]},
        "side_effects": [
            {"kind": "automation", "id": "auto-1", "before": {"llm_model": "gpt-4o"}},
        ],
        "comment": "Historical tweak",
        "restored_from": None,
        "actor_user_id": "admin-1",
        "actor_label": "Alice",
        "created_at": "2026-11-24T09:00:00Z",
        "payload": {
            "schema": CATALOG_SCHEMA,
            "models": {
                "gpt-5": {
                    "model_id": "gpt-5",
                    "provider": "openai",
                    "parameters": {
                        "max_tokens": 16384,
                        "api_key": "hist-api-key-secret",
                        "accessToken": "hist-camel-access-token",
                        "aws_session_token": "hist-aws-session-token",
                        "aws-session-token": "hist-hyphen-aws-token",
                        "clientSecret": "hist-client-secret",
                        "x-api-key": "hist-hyphen-api-key",
                        "default_headers": {"Authorization": "Bearer hist-bearer-secret"},
                    },
                }
            },
        },
    }

    orig_require_admin = dalton_router._require_catalog_admin
    orig_get_revision = db_catalog.get_catalog_revision
    orig_get_compiled = compiled_mod.get_compiled_catalog_async

    for oauth_enabled in (True, False):
        compiled = compile_catalog_from_payload(
            {"schema": CATALOG_SCHEMA},
            revision=12,
            oauth_sync_enabled=oauth_enabled,
            native_overlay_override={},
            upstream_models_override=_sample_upstream_models(),
            upstream_providers_override=_sample_upstream_providers(),
        )
        require_admin_mock = create_autospec(orig_require_admin, return_value=None)
        get_rev_mock = create_autospec(orig_get_revision, return_value=historical_row)
        get_compiled_mock = create_autospec(orig_get_compiled, return_value=compiled)
        monkeypatch.setattr(dalton_router, "_require_catalog_admin", require_admin_mock)
        monkeypatch.setattr(db_catalog, "get_catalog_revision", get_rev_mock)
        monkeypatch.setattr(compiled_mod, "get_compiled_catalog_async", get_compiled_mock)

        resp = Response()
        result = asyncio.run(
            dalton_router.get_team_model_revision_endpoint(
                revision=4, response=resp, user_id="admin-1"
            )
        )
        assert result["current_revision"] == 12
        assert resp.headers.get("X-Dalton-Catalog-Revision") == "12"
        record = result["revision_record"]
        assert "payload" not in record
        assert record["revision"] == 4
        assert record["action"] == "update_model"
        assert record["diff_summary"] == {"updated": "gpt-5", "fields": ["display_name"]}
        assert record["side_effects"] == historical_row["side_effects"]
        assert record["comment"] == "Historical tweak"
        assert record["actor_user_id"] == "admin-1"
        assert record["actor_label"] == "Alice"
        assert record["created_at"] == "2026-11-24T09:00:00Z"
        if oauth_enabled:
            assert record["target_keys"] == ["gpt-5", "claude-test-oauth"]
        else:
            assert record["target_keys"] == ["gpt-5"]

        serialized = str(result)
        for forbidden in (
            "api_key",
            "hist-api-key-secret",
            "accessToken",
            "aws_session_token",
            "aws-session-token",
            "clientSecret",
            "x-api-key",
            "hist-camel-access-token",
            "hist-aws-session-token",
            "hist-hyphen-aws-token",
            "hist-client-secret",
            "hist-hyphen-api-key",
            "hist-bearer-secret",
        ):
            assert forbidden not in serialized


def test_antigravity_max_output_tokens_update_applies_to_request_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_core.messages import HumanMessage

    from src.dalton.llm.providers.antigravity import build_antigravity_client

    antigravity_models = {
        **_sample_upstream_models(),
        "gemini-4-neon-high-oauth": {
            "model_id": "gemini-4-neon",
            "provider": "antigravity-oauth",
            "display_name": "Gemini 4 Neon",
            "visible": True,
            "context": 1000000,
            "input_modalities": ["text", "image"],
            "parameters": {"max_output_tokens": 65536},
        },
    }
    antigravity_providers = {
        "provider_config": {
            **_sample_upstream_providers()["provider_config"],
            "antigravity": {
                "sdk": "antigravity",
                "base_url": "https://daily-cloudcode-pa.googleapis.com",
                "env_key": None,
                "access_type": "oauth",
                "display_name": "Google Antigravity",
                "variants": {
                    "antigravity-oauth": {
                        "display_name": "Antigravity (Google Account)",
                        "billing": "subscription",
                    }
                },
            },
        },
        "models": _sample_upstream_providers()["models"],
    }
    state = _patch_tx_catalog_env(
        monkeypatch,
        initial_payload={"schema": CATALOG_SCHEMA},
        initial_revision=3,
        upstream_models=antigravity_models,
        upstream_providers=antigravity_providers,
    )

    parsed = CatalogModelUpsertInput.model_validate(
        {
            "parameters": {"max_output_tokens": 131072},
            "expected_revision": 3,
        }
    )
    res = asyncio.run(
        service_mod.update_catalog_model(
            actor_user_id="admin-1",
            model_key="gemini-4-neon-high-oauth",
            data=parsed,
        )
    )
    assert res["revision"] == 4

    saved_payload = state["saved_calls"][0]["payload"]
    compiled = compiled_mod.compile_catalog_from_payload(
        saved_payload,
        revision=4,
        oauth_sync_enabled=True,
        native_overlay_override={},
        upstream_models_override=antigravity_models,
        upstream_providers_override=antigravity_providers,
    )
    entry = compiled.model_config.get_model_config("gemini-4-neon-high-oauth")
    assert entry is not None
    assert entry["parameters"]["max_output_tokens"] == 131072
    assert "max_tokens" not in entry["parameters"]

    class _LLMStub:
        def __init__(self) -> None:
            self.model = entry["model_id"]
            self.parameters = dict(entry["parameters"])
            self.default_headers: dict[str, str] = {}

        def _resolve_api_key(self) -> str:
            return "oauth-access-token"

        def _resolve_base_url(self, key: str) -> dict[str, str]:
            return {key: "https://daily-cloudcode-pa.googleapis.com"}

    client = build_antigravity_client(_LLMStub())
    assert client.max_output_tokens == 131072
    req_payload = client._build_payload([HumanMessage(content="Hello")])
    assert req_payload["request"]["generationConfig"]["maxOutputTokens"] == 131072


def test_clearing_optional_notional_and_cash_cached_input_overrides_factory_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_overlay = {
        "models": {
            "gemini-4-neon-high-oauth": {
                "model_id": "gemini-4-neon",
                "provider": "claude-oauth",
                "display_name": "Gemini 4 Neon",
                "visible": True,
                "context": 1000000,
                "input_modalities": ["text"],
            }
        },
        "dalton_meta": {
            "gemini-4-neon-high-oauth": {
                "billing": "subscription",
                "tool_calling": True,
                "notional_pricing": {
                    "input": 1.5,
                    "cached_input": 0.15,
                    "output": 6.0,
                    "source_kind": "official",
                },
            }
        },
    }
    clean_upstream_models = _sample_upstream_models()
    clean_upstream_models["gpt-5"]["parameters"] = {"max_tokens": 16384}

    state = _patch_tx_catalog_env(
        monkeypatch,
        initial_payload={"schema": CATALOG_SCHEMA},
        initial_revision=5,
        upstream_models=clean_upstream_models,
        native_overlay=native_overlay,
    )

    # 1. Clear ONLY notional cached_input on factory subscription model
    sub_update = CatalogModelUpsertInput.model_validate(
        {
            "dalton_meta": {
                "notional_pricing": {
                    "input": 1.5,
                    "output": 6.0,
                    "cached_input": None,
                    "source_kind": "official",
                }
            },
            "expected_revision": 5,
        }
    )
    sub_res = asyncio.run(
        service_mod.update_catalog_model(
            actor_user_id="admin-1",
            model_key="gemini-4-neon-high-oauth",
            data=sub_update,
        )
    )
    assert sub_res["revision"] == 6

    # 2. Clear ONLY cash cached_input on factory cash model gpt-5
    cash_update = CatalogModelUpsertInput.model_validate(
        {
            "pricing": {
                "input": 2.5,
                "output": 10.0,
                "cached_input": None,
                "unit": "per_1m_tokens",
            },
            "expected_revision": 6,
        }
    )
    cash_res = asyncio.run(
        service_mod.update_catalog_model(
            actor_user_id="admin-1",
            model_key="gpt-5",
            data=cash_update,
        )
    )
    assert cash_res["revision"] == 7

    compiled = compiled_mod.compile_catalog_from_payload(state["row"]["payload"], revision=7)
    _patch_overview_dependencies(monkeypatch, compiled)
    overview = asyncio.run(service_mod.get_team_catalog_overview())
    by_key = {m["model_key"]: m for m in overview["models"]}

    sub_notional = by_key["gemini-4-neon-high-oauth"]["dalton_meta"]["notional_pricing"]
    assert sub_notional["input"] == 1.5
    assert sub_notional["output"] == 6.0
    assert "cached_input" not in sub_notional

    cash_pricing = by_key["gpt-5"]["pricing"]
    assert cash_pricing["input"] == 2.5
    assert cash_pricing["output"] == 10.0
    assert "cached_input" not in cash_pricing


def test_update_task_costs_bound_merges_partial_fields_into_current_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean_upstream_models = _sample_upstream_models()
    clean_upstream_models["gpt-5"]["parameters"] = {"max_tokens": 16384}

    initial_payload: dict[str, Any] = {
        "schema": CATALOG_SCHEMA,
        "task_costs": {
            "bounds": {
                "gpt-5": {"model": "claude-sonnet-4-5", "times": 0.75},
            }
        },
    }
    state = _patch_tx_catalog_env(
        monkeypatch,
        initial_payload=initial_payload,
        initial_revision=5,
        upstream_models=clean_upstream_models,
    )

    # 1. Admin B updates ONLY task_costs_bound.model (rev 5 -> 6)
    bob_update = CatalogModelUpsertInput.model_validate(
        {
            "task_costs_bound": {"model": "claude-opus-4-5"},
            "expected_revision": 5,
        }
    )
    bob_res = asyncio.run(
        service_mod.update_catalog_model(
            actor_user_id="admin-bob",
            model_key="gpt-5",
            data=bob_update,
        )
    )
    assert bob_res["revision"] == 6
    assert state["row"]["payload"]["task_costs"]["bounds"]["gpt-5"] == {
        "model": "claude-opus-4-5",
        "times": 0.75,
    }

    # 2. Admin A tries stale rev 5 with partial times -> rejected with revision_conflict before merge
    alice_stale = CatalogModelUpsertInput.model_validate(
        {
            "task_costs_bound": {"times": 1.5},
            "expected_revision": 5,
        }
    )
    with pytest.raises(CatalogConflictError) as exc_info:
        asyncio.run(
            service_mod.update_catalog_model(
                actor_user_id="admin-alice",
                model_key="gpt-5",
                data=alice_stale,
            )
        )
    assert exc_info.value.code == "revision_conflict"
    assert state["row"]["payload"]["task_costs"]["bounds"]["gpt-5"] == {
        "model": "claude-opus-4-5",
        "times": 0.75,
    }

    # 3. Admin A retries at rev 6 with ONLY times -> merges into Bob's updated model
    alice_retry = CatalogModelUpsertInput.model_validate(
        {
            "task_costs_bound": {"times": 1.5},
            "expected_revision": 6,
        }
    )
    alice_res = asyncio.run(
        service_mod.update_catalog_model(
            actor_user_id="admin-alice",
            model_key="gpt-5",
            data=alice_retry,
        )
    )
    assert alice_res["revision"] == 7
    assert state["row"]["payload"]["task_costs"]["bounds"]["gpt-5"] == {
        "model": "claude-opus-4-5",
        "times": 1.5,
    }

    # 4. Clearing task_costs_bound sets explicit None; subsequent partial times-only patch fails validation
    clear_bound = CatalogModelUpsertInput.model_validate(
        {
            "task_costs_bound": None,
            "expected_revision": 7,
        }
    )
    clear_res = asyncio.run(
        service_mod.update_catalog_model(
            actor_user_id="admin-bob",
            model_key="gpt-5",
            data=clear_bound,
        )
    )
    assert clear_res["revision"] == 8
    assert state["row"]["payload"]["task_costs"]["bounds"]["gpt-5"] is None

    incomplete_after_clear = CatalogModelUpsertInput.model_validate(
        {
            "task_costs_bound": {"times": 2.0},
            "expected_revision": 8,
        }
    )
    with pytest.raises(CatalogValidationError) as val_exc:
        asyncio.run(
            service_mod.update_catalog_model(
                actor_user_id="admin-alice",
                model_key="gpt-5",
                data=incomplete_after_clear,
            )
        )
    assert val_exc.value.code == "invalid_task_costs_bound"


def test_partial_null_notional_rates_context_clear_and_tiered_cash_pricing_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream_models = _sample_upstream_models()
    upstream_models["gpt-5"]["parameters"] = {"max_tokens": 16384}
    upstream_models["grok-4.7"] = {
        "model_id": "grok-4.7",
        "provider": "openai",
        "display_name": "Grok 4.7",
        "visible": True,
        "context": 2000000,
        "input_modalities": ["text", "image"],
    }
    upstream_providers = _sample_upstream_providers()
    upstream_providers["models"]["openai"].append(
        {
            "id": "grok-4.7",
            "name": "Grok 4.7",
            "pricing": {
                "input_tiers": [
                    {"max_tokens": 200000, "rate": 2.0, "cached_input": 0.5},
                    {"max_tokens": None, "rate": 4.0, "cached_input": 1.0},
                ],
                "output_tiers": [
                    {"max_tokens": 200000, "rate": 6.0},
                    {"max_tokens": None, "rate": 12.0},
                ],
            },
        }
    )
    native_overlay = {
        "models": {
            "gemini-4-neon-high-oauth": {
                "model_id": "gemini-4-neon",
                "provider": "claude-oauth",
                "display_name": "Gemini 4 Neon",
                "visible": True,
                "context": 1000000,
                "input_modalities": ["text"],
            }
        },
        "dalton_meta": {
            "gemini-4-neon-high-oauth": {
                "billing": "subscription",
                "tool_calling": True,
                "notional_pricing": {
                    "input": 1.5,
                    "cached_input": 0.2,
                    "output": 6.0,
                    "source_kind": "third_party",
                    "note": "Added by Bob",
                },
            }
        },
    }

    state = _patch_tx_catalog_env(
        monkeypatch,
        initial_payload={"schema": CATALOG_SCHEMA},
        initial_revision=5,
        upstream_models=upstream_models,
        upstream_providers=upstream_providers,
        native_overlay=native_overlay,
    )

    # 1. Per-field null clearing of notional input & output preserves Bob's cached_input & price source
    res1 = asyncio.run(
        service_mod.update_catalog_model(
            actor_user_id="admin-alice",
            model_key="gemini-4-neon-high-oauth",
            data=CatalogModelUpsertInput.model_validate(
                {
                    "dalton_meta": {
                        "notional_pricing": {
                            "input": None,
                            "output": None,
                        }
                    },
                    "expected_revision": 5,
                }
            ),
        )
    )
    assert res1["revision"] == 6

    # 2. Clearing context on gpt-5 removes context in compiled overview
    res2 = asyncio.run(
        service_mod.update_catalog_model(
            actor_user_id="admin-alice",
            model_key="gpt-5",
            data=CatalogModelUpsertInput.model_validate(
                {
                    "context": None,
                    "expected_revision": 6,
                }
            ),
        )
    )
    assert res2["revision"] == 7

    # 3. Tiered-only cash model accepts pricing_meta-only and output-only updates while preserving tiers
    res3 = asyncio.run(
        service_mod.update_catalog_model(
            actor_user_id="admin-alice",
            model_key="grok-4.7",
            data=CatalogModelUpsertInput.model_validate(
                {
                    "pricing_meta": {"source_kind": "third_party", "verified_at": "2026-11-26"},
                    "expected_revision": 7,
                }
            ),
        )
    )
    assert res3["revision"] == 8

    res4 = asyncio.run(
        service_mod.update_catalog_model(
            actor_user_id="admin-alice",
            model_key="grok-4.7",
            data=CatalogModelUpsertInput.model_validate(
                {
                    "pricing": {"output": 14.0},
                    "expected_revision": 8,
                }
            ),
        )
    )
    assert res4["revision"] == 9

    compiled = compiled_mod.compile_catalog_from_payload(state["row"]["payload"], revision=9)
    _patch_overview_dependencies(monkeypatch, compiled)
    overview = asyncio.run(service_mod.get_team_catalog_overview())
    by_key = {m["model_key"]: m for m in overview["models"]}

    sub_np = by_key["gemini-4-neon-high-oauth"]["dalton_meta"]["notional_pricing"]
    assert "input" not in sub_np
    assert "output" not in sub_np
    assert sub_np["cached_input"] == 0.2
    assert sub_np["source_kind"] == "third_party"
    assert sub_np["note"] == "Added by Bob"

    assert by_key["gpt-5"]["context"] is None

    grok = by_key["grok-4.7"]
    assert grok["pricing_meta"] == {"source_kind": "third_party", "verified_at": "2026-11-26"}
    assert grok["pricing"]["output"] == 14.0
    assert "input" not in grok["pricing"]
    assert len(grok["pricing"]["input_tiers"]) == 2
    assert len(grok["pricing"]["output_tiers"]) == 2
