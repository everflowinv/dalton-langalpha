"""Unit tests ensuring team-shared BYOK keys are never sent to member-supplied endpoints.

All test doubles use ``unittest.mock.create_autospec`` to preserve signatures.
"""

from __future__ import annotations

import socket
from typing import Any
from unittest.mock import create_autospec

import httpx
import httpx2
import pytest
from fastapi import HTTPException

import src.dalton.llm.complete as complete_mod
import src.dalton.settings as settings_mod
import src.llms.api_call as api_call_mod
import src.llms.llm as llm_mod
import src.server.app.users as users_app
import src.server.services.account as account_svc
import src.server.services.features as features_svc
import src.server.services.llm.clients as clients_mod
import src.server.services.llm.config as config_mod
import src.server.services.llm.custom_catalog as custom_catalog_mod
import src.server.services.llm.service as service_mod
import src.server.services.llm.user_models as user_models_mod
import src.server.services.oauth_health as oauth_health_mod
import src.server.services.user_skills as user_skills_svc
from ptc_agent.config import (
    AgentConfig,
    DaytonaConfig,
    FilesystemConfig,
    LLMConfig,
    LoggingConfig,
    MCPConfig,
    SandboxConfig,
    SecurityConfig,
)
from src.server.models.user import ModelPreference, UserPreferencesUpdate
from src.server.services.llm.byok import byok_candidates
from src.server.services.policy import Policy

HOLDER_ID = "991a99ad-8ff2-4bd8-bf08-e5963a248798"
SECOND_PM_ID = "22222222-2222-4222-8222-222222222222"
MEMBER_ID = "33333333-3333-4333-8333-333333333333"
HOLDER_SECRET_KEY = "sk-holder-secret-do-not-exfiltrate-9999"
HOLDER_SAVED_BASE_URL = "https://holder-gateway.corp.example/v1"
ATTACKER_URL = "https://attacker.public.example/v1"

_ORIG_LOAD_BASE_CONFIG = settings_mod._load_base_dalton_config


@pytest.fixture(autouse=True)
def _reset_dalton_config():
    settings_mod.get_dalton_config.cache_clear()
    yield
    settings_mod.get_dalton_config.cache_clear()


def _configure_team_sharing(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sharing: str = "team",
    auth_mode: str = "supabase",
    pm_ids: str | None = f"{HOLDER_ID},{SECOND_PM_ID}",
    host_mode: str = "oss",
) -> None:
    monkeypatch.setenv("HOST_MODE", host_mode)
    monkeypatch.setenv("DALTON_AUTH_MODE", auth_mode)
    if pm_ids is None:
        monkeypatch.delenv("DALTON_PM_USER_IDS", raising=False)
    else:
        monkeypatch.setenv("DALTON_PM_USER_IDS", pm_ids)
    settings_mod.get_dalton_config.cache_clear()

    if sharing != "team":
        base_cfg = settings_mod.DaltonConfig(
            enabled=True,
            credentials=settings_mod.DaltonCredentialsConfig(sharing=sharing),  # type: ignore[arg-type]
        )
        monkeypatch.setattr(
            settings_mod,
            "_load_base_dalton_config",
            create_autospec(_ORIG_LOAD_BASE_CONFIG, return_value=base_cfg),
        )
    else:
        monkeypatch.setattr(settings_mod, "_load_base_dalton_config", _ORIG_LOAD_BASE_CONFIG)
        settings_mod.get_dalton_config.cache_clear()


def _install_network_tripwire(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail loudly if any DNS lookup or HTTP client send is attempted."""
    events: list[str] = []

    def _boom_dns(*args: Any, **kwargs: Any) -> Any:
        events.append(f"dns:{args}")
        raise AssertionError(f"Unexpected DNS lookup: {args}")

    def _boom_sync_send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        events.append(f"sync_http:{request.url}")
        raise AssertionError(f"Unexpected sync HTTP request to {request.url}")

    async def _boom_async_send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        events.append(f"async_http:{request.url}")
        raise AssertionError(f"Unexpected async HTTP request to {request.url}")

    monkeypatch.setattr(socket, "getaddrinfo", create_autospec(socket.getaddrinfo, side_effect=_boom_dns))
    monkeypatch.setattr(httpx.Client, "send", create_autospec(httpx.Client.send, side_effect=_boom_sync_send))
    monkeypatch.setattr(httpx.AsyncClient, "send", create_autospec(httpx.AsyncClient.send, side_effect=_boom_async_send))
    monkeypatch.setattr(httpx2.Client, "send", create_autospec(httpx2.Client.send, side_effect=_boom_sync_send))
    monkeypatch.setattr(httpx2.AsyncClient, "send", create_autospec(httpx2.AsyncClient.send, side_effect=_boom_async_send))
    return events


def _minimal_agent_config(default_model: str = "gpt-6.1-terra") -> AgentConfig:
    return AgentConfig(
        llm=LLMConfig(name=default_model, background=default_model),
        security=SecurityConfig(),
        logging=LoggingConfig(),
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="k")),
        mcp=MCPConfig(),
        filesystem=FilesystemConfig(),
    )


def _patch_holder_prefs(
    monkeypatch: pytest.MonkeyPatch,
    holder_pref: dict[str, Any] | None = None,
    *,
    extra_prefs: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Stub ``user_models.get_model_preference`` for holder (and optional other users)."""
    prefs_by_user: dict[str, dict[str, Any]] = {HOLDER_ID: dict(holder_pref or {})}
    if extra_prefs:
        for uid, pref in extra_prefs.items():
            prefs_by_user[uid] = dict(pref)

    async def _get_pref(user_id: str) -> dict[str, Any]:
        return dict(prefs_by_user.get(user_id, {}))

    monkeypatch.setattr(
        user_models_mod,
        "get_model_preference",
        create_autospec(user_models_mod.get_model_preference, side_effect=_get_pref),
    )


def _byok_cache_for(provider: str, entry: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    mc = llm_mod.LLM.get_model_config()
    cache: dict[str, dict[str, Any] | None] = {
        slug: None for slug in byok_candidates(provider, mc)
    }
    cache[provider] = entry
    return cache


def _patch_resolve_llm_config_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_pref_by_user: dict[str, dict[str, Any]],
    byok_configs: dict[str, dict[str, Any]],
) -> None:
    """Stub DB/policy/skill lookups for ``resolve_llm_config`` using ``create_autospec``."""
    unrestricted = Policy()

    async def _require_policy(user_id: str) -> Policy:
        return unrestricted

    async def _get_model_pref(user_id: str) -> dict[str, Any]:
        return dict(model_pref_by_user.get(user_id, {}))

    async def _effective_flags(user_id: str, overrides: dict[str, Any]) -> dict[str, bool]:
        return {}

    async def _load_skills(user_id: str, workspace_id: str | None) -> Any:
        return user_skills_svc.EMPTY_USER_SKILL_BUNDLE

    async def _get_byok_one(user_id: str, provider: str) -> dict[str, Any] | None:
        return byok_configs.get(provider)

    async def _get_byok_batch(user_id: str, providers: list[str]) -> dict[str, dict[str, Any]]:
        return {p: byok_configs[p] for p in providers if p in byok_configs}

    monkeypatch.setattr(
        config_mod,
        "require_policy",
        create_autospec(account_svc.require_policy, side_effect=_require_policy),
    )
    monkeypatch.setattr(
        clients_mod,
        "require_policy",
        create_autospec(account_svc.require_policy, side_effect=_require_policy),
    )
    monkeypatch.setattr(
        user_models_mod,
        "get_model_preference",
        create_autospec(user_models_mod.get_model_preference, side_effect=_get_model_pref),
    )
    monkeypatch.setattr(
        config_mod,
        "effective_flags_for_user",
        create_autospec(features_svc.effective_flags_for_user, side_effect=_effective_flags),
    )
    monkeypatch.setattr(
        user_skills_svc,
        "load_user_skill_bundle",
        create_autospec(user_skills_svc.load_user_skill_bundle, side_effect=_load_skills),
    )
    monkeypatch.setattr(
        user_skills_svc,
        "shared_computer_withheld",
        create_autospec(user_skills_svc.shared_computer_withheld, return_value=frozenset()),
    )

    import src.server.database.api_keys as api_keys_db

    monkeypatch.setattr(
        api_keys_db,
        "get_byok_config_for_provider",
        create_autospec(api_keys_db.get_byok_config_for_provider, side_effect=_get_byok_one),
    )
    monkeypatch.setattr(
        api_keys_db,
        "get_byok_configs_for_providers",
        create_autospec(api_keys_db.get_byok_configs_for_providers, side_effect=_get_byok_batch),
    )


# ---------------------------------------------------------------------------
# 1. Runtime rejection in resolve_byok_llm_client / resolve_model_client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params,expected_key",
    [
        ({"base_url": ATTACKER_URL}, "parameters.base_url"),
        ({"openai_api_base": ATTACKER_URL}, "parameters.openai_api_base"),
        ({"api_base": ATTACKER_URL}, "parameters.api_base"),
        ({"openrouter_api_base": ATTACKER_URL}, "parameters.openrouter_api_base"),
        ({"anthropic_api_url": ATTACKER_URL}, "parameters.anthropic_api_url"),
        ({"client_options": {"api_endpoint": ATTACKER_URL}}, "parameters.client_options"),
        ({"model_kwargs": {"openai_api_base": ATTACKER_URL}}, "parameters.model_kwargs.openai_api_base"),
        ({"model_kwargs": {"base_url": ATTACKER_URL}}, "parameters.model_kwargs.base_url"),
        ({"openai_proxy": "http://attacker.public.example:8080"}, "parameters.openai_proxy"),
        ({"anthropic_proxy": "http://attacker.public.example:8080"}, "parameters.anthropic_proxy"),
        ({"client_args": {"transport": None}}, "parameters.client_args"),
        ({"model_kwargs": {"openai_proxy": "http://attacker.public.example:8080"}}, "parameters.model_kwargs.openai_proxy"),
        ({"vertexai": True}, "parameters.vertexai"),
        ({"project": "evil-proj"}, "parameters.project"),
        ({"location": "attacker.public.example/x?"}, "parameters.location"),
        ({"credentials": {"type": "service_account"}}, "parameters.credentials"),
        ({"model_kwargs": {"location": "attacker.public.example/x?"}}, "parameters.model_kwargs.location"),
    ],
)
async def test_member_custom_model_with_routing_params_rejected_at_runtime(
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
    expected_key: str,
) -> None:
    _configure_team_sharing(monkeypatch, sharing="team")
    net_events = _install_network_tripwire(monkeypatch)
    _patch_holder_prefs(monkeypatch, {})

    custom_entry = {
        "name": "exfil-model",
        "model_id": "gpt-4o",
        "provider": "openai",
        "parameters": params,
    }
    pref_cache = {"custom_models": [custom_entry]}
    byok_cache = _byok_cache_for("openai", {"api_key": HOLDER_SECRET_KEY, "base_url": None})

    mock_create_custom = create_autospec(llm_mod.create_llm_from_custom)
    mock_create_llm = create_autospec(llm_mod.create_llm)
    monkeypatch.setattr(llm_mod, "create_llm_from_custom", mock_create_custom)
    monkeypatch.setattr(llm_mod, "create_llm", mock_create_llm)

    with pytest.raises(HTTPException) as exc_info:
        await clients_mod.resolve_byok_llm_client(
            MEMBER_ID,
            "exfil-model",
            is_byok=True,
            _pref_cache=pref_cache,
            _byok_cache=byok_cache,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["type"] == "base_url_blocked"
    assert expected_key in exc_info.value.detail["message"]
    mock_create_custom.assert_not_called()
    mock_create_llm.assert_not_called()
    assert net_events == []


@pytest.mark.asyncio
async def test_second_pm_not_holder_also_rejected_when_overriding_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the credential holder itself (credential_user_id(u) == u) may set custom endpoints."""
    _configure_team_sharing(monkeypatch, sharing="team")
    net_events = _install_network_tripwire(monkeypatch)
    _patch_holder_prefs(monkeypatch, {})

    pref_cache = {
        "custom_models": [
            {
                "name": "pm2-custom",
                "model_id": "gpt-4o",
                "provider": "openai",
                "parameters": {"base_url": ATTACKER_URL},
            }
        ]
    }
    byok_cache = _byok_cache_for("openai", {"api_key": HOLDER_SECRET_KEY, "base_url": None})
    mock_create_custom = create_autospec(llm_mod.create_llm_from_custom)
    monkeypatch.setattr(llm_mod, "create_llm_from_custom", mock_create_custom)

    with pytest.raises(HTTPException) as exc_info:
        await clients_mod.resolve_byok_llm_client(
            SECOND_PM_ID,
            "pm2-custom",
            is_byok=True,
            _pref_cache=pref_cache,
            _byok_cache=byok_cache,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["type"] == "base_url_blocked"
    mock_create_custom.assert_not_called()
    assert net_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"base_url": ATTACKER_URL},
        {"model_kwargs": {"openai_api_base": ATTACKER_URL}},
    ],
)
async def test_holder_can_use_own_custom_model_with_endpoint_params(
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
) -> None:
    _configure_team_sharing(monkeypatch, sharing="team")
    net_events = _install_network_tripwire(monkeypatch)

    custom_entry = {
        "name": "holder-custom",
        "model_id": "gpt-4o",
        "provider": "openai",
        "parameters": params,
    }
    pref_cache = {"custom_models": [custom_entry]}
    byok_cache = _byok_cache_for("openai", {"api_key": HOLDER_SECRET_KEY, "base_url": None})
    sentinel_client = object()
    mock_create_custom = create_autospec(llm_mod.create_llm_from_custom, return_value=sentinel_client)
    monkeypatch.setattr(llm_mod, "create_llm_from_custom", mock_create_custom)

    client = await clients_mod.resolve_byok_llm_client(
        HOLDER_ID,
        "holder-custom",
        is_byok=True,
        _pref_cache=pref_cache,
        _byok_cache=byok_cache,
    )

    assert client is sentinel_client
    mock_create_custom.assert_called_once()
    assert mock_create_custom.call_args.kwargs["api_key"] == HOLDER_SECRET_KEY
    assert net_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sharing_kwargs",
    [
        {"sharing": "off", "pm_ids": f"{HOLDER_ID},{SECOND_PM_ID}"},
        {"sharing": "team", "pm_ids": None},
    ],
)
@pytest.mark.parametrize(
    "params",
    [
        {"base_url": ATTACKER_URL},
        {"model_kwargs": {"openai_api_base": ATTACKER_URL}},
    ],
)
async def test_member_allowed_when_team_sharing_disabled(
    monkeypatch: pytest.MonkeyPatch,
    sharing_kwargs: dict[str, Any],
    params: dict[str, Any],
) -> None:
    _configure_team_sharing(monkeypatch, **sharing_kwargs)
    net_events = _install_network_tripwire(monkeypatch)

    custom_entry = {
        "name": "personal-custom",
        "model_id": "gpt-4o",
        "provider": "openai",
        "parameters": params,
    }
    pref_cache = {"custom_models": [custom_entry]}
    byok_cache = _byok_cache_for("openai", {"api_key": "sk-member-own-key", "base_url": None})
    sentinel_client = object()
    mock_create_custom = create_autospec(llm_mod.create_llm_from_custom, return_value=sentinel_client)
    monkeypatch.setattr(llm_mod, "create_llm_from_custom", mock_create_custom)

    client = await clients_mod.resolve_byok_llm_client(
        MEMBER_ID,
        "personal-custom",
        is_byok=True,
        _pref_cache=pref_cache,
        _byok_cache=byok_cache,
    )

    assert client is sentinel_client
    mock_create_custom.assert_called_once()
    assert mock_create_custom.call_args.kwargs["api_key"] == "sk-member-own-key"
    assert net_events == []


# ---------------------------------------------------------------------------
# 2. Custom provider & base_url provenance checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_custom_provider_uses_only_holder_saved_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_team_sharing(monkeypatch, sharing="team")
    net_events = _install_network_tripwire(monkeypatch)
    _patch_holder_prefs(
        monkeypatch,
        {"custom_providers": [{"name": "corp-gw", "parent_provider": "openai"}]},
    )

    pref_cache = {
        "custom_providers": [{"name": "corp-gw", "parent_provider": "openai"}],
        "custom_models": [
            {
                "name": "corp-model",
                "model_id": "gpt-4o",
                "provider": "corp-gw",
                "parameters": {"temperature": 0.2},
            }
        ],
    }
    byok_cache = {
        "corp-gw": {"api_key": HOLDER_SECRET_KEY, "base_url": HOLDER_SAVED_BASE_URL},
    }
    sentinel_client = object()
    mock_create_custom = create_autospec(llm_mod.create_llm_from_custom, return_value=sentinel_client)
    monkeypatch.setattr(llm_mod, "create_llm_from_custom", mock_create_custom)

    client = await clients_mod.resolve_byok_llm_client(
        MEMBER_ID,
        "corp-model",
        is_byok=True,
        _pref_cache=pref_cache,
        _byok_cache=byok_cache,
    )

    assert client is sentinel_client
    mock_create_custom.assert_called_once()
    assert mock_create_custom.call_args.kwargs["base_url"] == HOLDER_SAVED_BASE_URL
    assert mock_create_custom.call_args.kwargs["api_key"] == HOLDER_SECRET_KEY
    assert mock_create_custom.call_args.kwargs["follow_redirects"] is False
    assert net_events == []


@pytest.mark.asyncio
async def test_member_custom_provider_with_embedded_base_url_rejected_at_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if a member's custom_providers row carries a base_url in DB, runtime refuses it."""
    _configure_team_sharing(monkeypatch, sharing="team")
    net_events = _install_network_tripwire(monkeypatch)
    _patch_holder_prefs(
        monkeypatch,
        {"custom_providers": [{"name": "corp-gw", "parent_provider": "openai"}]},
    )

    pref_cache = {
        "custom_providers": [
            {"name": "corp-gw", "parent_provider": "openai", "base_url": ATTACKER_URL},
        ],
        "custom_models": [
            {"name": "corp-model", "model_id": "gpt-4o", "provider": "corp-gw"},
        ],
    }
    byok_cache = {
        "corp-gw": {"api_key": HOLDER_SECRET_KEY, "base_url": HOLDER_SAVED_BASE_URL},
    }
    mock_create_custom = create_autospec(llm_mod.create_llm_from_custom)
    monkeypatch.setattr(llm_mod, "create_llm_from_custom", mock_create_custom)

    with pytest.raises(HTTPException) as exc_info:
        await clients_mod.resolve_byok_llm_client(
            MEMBER_ID,
            "corp-model",
            is_byok=True,
            _pref_cache=pref_cache,
            _byok_cache=byok_cache,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["type"] == "base_url_blocked"
    assert "base_url" in exc_info.value.detail["message"]
    mock_create_custom.assert_not_called()
    assert net_events == []


@pytest.mark.asyncio
async def test_member_custom_provider_shadowing_builtin_cannot_redirect_holder_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member custom_provider named after a built-in provider cannot redirect the holder's key."""
    _configure_team_sharing(monkeypatch, sharing="team")
    net_events = _install_network_tripwire(monkeypatch)
    _patch_holder_prefs(monkeypatch, {})

    pref_cache = {
        "custom_providers": [
            {"name": "openai", "parent_provider": "deepseek"},
        ],
        "custom_models": [
            {"name": "redirected", "model_id": "gpt-4o", "provider": "openai"},
        ],
    }
    # Holder's OpenAI key has no custom base_url (uses official OpenAI default None).
    byok_cache = _byok_cache_for("openai", {"api_key": HOLDER_SECRET_KEY, "base_url": None})
    mock_create_custom = create_autospec(llm_mod.create_llm_from_custom)
    monkeypatch.setattr(llm_mod, "create_llm_from_custom", mock_create_custom)

    with pytest.raises(HTTPException) as exc_info:
        await clients_mod.resolve_byok_llm_client(
            MEMBER_ID,
            "redirected",
            is_byok=True,
            _pref_cache=pref_cache,
            _byok_cache=byok_cache,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["type"] == "base_url_blocked"
    mock_create_custom.assert_not_called()
    assert net_events == []


@pytest.mark.asyncio
async def test_member_tampered_route_base_url_rejected_before_client_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If any route yields a base_url not from holder's byok_config or manifest, refuse it."""
    _configure_team_sharing(monkeypatch, sharing="team")
    net_events = _install_network_tripwire(monkeypatch)

    tampered_route = clients_mod.OwnKeyRoute(
        key={"api_key": HOLDER_SECRET_KEY, "base_url": None},
        base_url=ATTACKER_URL,
        provider="openai",
        custom_config=None,
        holding="openai",
    )
    monkeypatch.setattr(
        clients_mod,
        "byok_key",
        create_autospec(clients_mod.byok_key, return_value=tampered_route),
    )
    mock_create_llm = create_autospec(llm_mod.create_llm)
    monkeypatch.setattr(llm_mod, "create_llm", mock_create_llm)

    with pytest.raises(HTTPException) as exc_info:
        await clients_mod.resolve_byok_llm_client(
            MEMBER_ID,
            "gpt-6.1-terra",
            is_byok=True,
            _pref_cache={},
            _byok_cache={"openai": {"api_key": HOLDER_SECRET_KEY, "base_url": None}},
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["type"] == "base_url_blocked"
    mock_create_llm.assert_not_called()
    assert net_events == []


# ---------------------------------------------------------------------------
# 3. Save-time rejection (custom_catalog + users.update_preferences)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"base_url": ATTACKER_URL},
        {"model_kwargs": {"openai_api_base": ATTACKER_URL}},
        {"openai_proxy": "http://attacker.public.example:8080"},
        {"client_options": {"api_endpoint": ATTACKER_URL}},
        {"vertexai": True, "location": "attacker.public.example/x?"},
    ],
)
async def test_member_save_custom_model_with_routing_params_rejected(
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
) -> None:
    _configure_team_sharing(monkeypatch, sharing="team", host_mode="oss")
    net_events = _install_network_tripwire(monkeypatch)

    entry = {
        "name": "evil-save",
        "model_id": "gpt-4o",
        "provider": "openai",
        "parameters": params,
    }
    with pytest.raises(HTTPException) as exc_info:
        await custom_catalog_mod.refuse_custom_model_endpoints([entry], user_id=MEMBER_ID)

    assert exc_info.value.status_code == 400
    assert "custom_models[0]:" in exc_info.value.detail
    assert net_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cp_entry",
    [
        {"name": "evil-gw", "parent_provider": "openai", "base_url": ATTACKER_URL},
        {"name": "evil-gw", "parent_provider": "openai", "parameters": {"base_url": ATTACKER_URL}},
        {
            "name": "evil-gw",
            "parent_provider": "openai",
            "parameters": {"model_kwargs": {"openai_api_base": ATTACKER_URL}},
        },
        {"name": "evil-gw", "parent_provider": "openai", "openai_proxy": "http://attacker.public.example:8080"},
    ],
)
async def test_member_save_custom_provider_with_routing_params_rejected(
    monkeypatch: pytest.MonkeyPatch,
    cp_entry: dict[str, Any],
) -> None:
    _configure_team_sharing(monkeypatch, sharing="team", host_mode="oss")

    with pytest.raises(HTTPException) as exc_info:
        custom_catalog_mod.validate_custom_providers([cp_entry], user_id=MEMBER_ID)

    assert exc_info.value.status_code == 400
    assert "custom_providers[0]:" in exc_info.value.detail


@pytest.mark.asyncio
async def test_holder_and_sharing_off_member_can_save_custom_model_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = {
        "name": "custom-gw-model",
        "model_id": "gpt-4o",
        "provider": "openai",
        "parameters": {
            "base_url": ATTACKER_URL,
            "model_kwargs": {"openai_api_base": ATTACKER_URL},
        },
    }

    _configure_team_sharing(monkeypatch, sharing="team", host_mode="oss")
    await custom_catalog_mod.refuse_custom_model_endpoints([entry], user_id=HOLDER_ID)

    _configure_team_sharing(monkeypatch, sharing="off", host_mode="oss")
    await custom_catalog_mod.refuse_custom_model_endpoints([entry], user_id=MEMBER_ID)


@pytest.mark.asyncio
async def test_update_preferences_endpoint_rejects_member_before_db_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_team_sharing(monkeypatch, sharing="team", host_mode="oss")
    net_events = _install_network_tripwire(monkeypatch)

    monkeypatch.setattr(
        users_app,
        "db_get_user",
        create_autospec(users_app.db_get_user, return_value={"user_id": MEMBER_ID}),
    )
    monkeypatch.setattr(
        users_app,
        "db_get_user_preferences",
        create_autospec(users_app.db_get_user_preferences, return_value={"model_preference": {}}),
    )
    monkeypatch.setattr(
        account_svc,
        "require_policy",
        create_autospec(account_svc.require_policy, return_value=Policy()),
    )
    mock_upsert = create_autospec(users_app.upsert_user_preferences)
    monkeypatch.setattr(users_app, "upsert_user_preferences", mock_upsert)

    bad_model_req = UserPreferencesUpdate(
        model_preference=ModelPreference(
            custom_models=[
                {
                    "name": "member-exfil",
                    "model_id": "gpt-4o",
                    "provider": "openai",
                    "parameters": {"base_url": ATTACKER_URL},
                }
            ]
        )
    )
    with pytest.raises(HTTPException) as exc_info:
        await users_app.update_preferences(bad_model_req, user_id=MEMBER_ID)
    assert exc_info.value.status_code == 400
    assert "parameters.base_url" in exc_info.value.detail
    mock_upsert.assert_not_called()

    bad_provider_req = UserPreferencesUpdate(
        model_preference=ModelPreference(
            custom_providers=[
                {
                    "name": "member-gw",
                    "parent_provider": "openai",
                    "base_url": ATTACKER_URL,
                }
            ]
        )
    )
    with pytest.raises(HTTPException) as exc_info2:
        await users_app.update_preferences(bad_provider_req, user_id=MEMBER_ID)
    assert exc_info2.value.status_code == 400
    assert "custom_providers[0]:" in exc_info2.value.detail
    mock_upsert.assert_not_called()
    assert net_events == []


# ---------------------------------------------------------------------------
# 4. resolve_llm_config, LLMService.complete, and dalton_complete paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_llm_config_rejects_member_main_model_and_skips_role_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_team_sharing(monkeypatch, sharing="team", host_mode="oss")
    net_events = _install_network_tripwire(monkeypatch)

    bad_custom = {
        "name": "evil-main",
        "model_id": "gpt-4o",
        "provider": "openai",
        "parameters": {"model_kwargs": {"openai_api_base": ATTACKER_URL}},
    }
    _patch_resolve_llm_config_deps(
        monkeypatch,
        model_pref_by_user={
            MEMBER_ID: {
                "custom_models": [bad_custom],
                "fallback_models": ["evil-main"],
                "fetch_model": "evil-main",
            }
        },
        byok_configs={"openai": {"api_key": HOLDER_SECRET_KEY, "base_url": None}},
    )
    mock_create_custom = create_autospec(llm_mod.create_llm_from_custom)
    monkeypatch.setattr(llm_mod, "create_llm_from_custom", mock_create_custom)

    # 1. Main model fails loudly with HTTP 400 and constructs no client.
    with pytest.raises(HTTPException) as exc_info:
        await config_mod.resolve_llm_config(
            base_config=_minimal_agent_config(),
            user_id=MEMBER_ID,
            request_model="evil-main",
            is_byok=True,
            slot="primary",
            user_facing=False,
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["type"] == "base_url_blocked"
    mock_create_custom.assert_not_called()

    # 2. When main model is built-in, a bad custom fallback/fetch role never constructs a client.
    fake_builtin_client = create_autospec(llm_mod.BaseChatModel, instance=True)
    fake_builtin_client.metadata = {}
    fake_builtin_client.model_copy.return_value = fake_builtin_client
    mock_create_llm = create_autospec(llm_mod.create_llm, return_value=fake_builtin_client)
    monkeypatch.setattr(llm_mod, "create_llm", mock_create_llm)

    resolved = await config_mod.resolve_llm_config(
        base_config=_minimal_agent_config(),
        user_id=MEMBER_ID,
        request_model="gpt-6.1-terra",
        is_byok=True,
        slot="primary",
        user_facing=False,
    )
    mock_create_custom.assert_not_called()
    assert "evil-main" not in (resolved.fallback_llm_names or [])
    assert resolved.llm_client is fake_builtin_client
    assert net_events == []


@pytest.mark.asyncio
async def test_llm_service_complete_and_dalton_complete_reject_member_custom_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_team_sharing(monkeypatch, sharing="team", host_mode="oss")
    net_events = _install_network_tripwire(monkeypatch)

    bad_custom = {
        "name": "evil-oneshot",
        "model_id": "gpt-4o",
        "provider": "openai",
        "parameters": {"base_url": ATTACKER_URL},
    }
    _patch_resolve_llm_config_deps(
        monkeypatch,
        model_pref_by_user={MEMBER_ID: {"custom_models": [bad_custom]}},
        byok_configs={"openai": {"api_key": HOLDER_SECRET_KEY, "base_url": None}},
    )
    mock_create_custom = create_autospec(llm_mod.create_llm_from_custom)
    mock_make_api_call = create_autospec(api_call_mod.make_api_call)
    monkeypatch.setattr(llm_mod, "create_llm_from_custom", mock_create_custom)
    monkeypatch.setattr(service_mod, "make_api_call", mock_make_api_call)

    svc = service_mod.LLMService(agent_config=_minimal_agent_config())

    # LLMService.complete directly
    with pytest.raises(HTTPException) as exc_info:
        await svc.complete(
            user_id=MEMBER_ID,
            user_prompt="ping",
            request_model="evil-oneshot",
            is_byok=True,
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["type"] == "base_url_blocked"
    mock_create_custom.assert_not_called()
    mock_make_api_call.assert_not_called()

    # dalton_complete (return envelope)
    res = await complete_mod.dalton_complete(
        llm_service=svc,
        user_id=MEMBER_ID,
        user_prompt="ping",
        model="evil-oneshot",
        is_byok=True,
        skip_journal=True,
    )
    assert res.ok is False
    assert res.status == "failed"
    assert res.error_kind == "http_400"
    assert "parameters.base_url" in (res.error or "")
    mock_create_custom.assert_not_called()
    mock_make_api_call.assert_not_called()

    # dalton_complete with raise_on_error=True
    with pytest.raises(complete_mod.JournalError) as j_exc:
        await complete_mod.dalton_complete(
            llm_service=svc,
            user_id=MEMBER_ID,
            user_prompt="ping",
            model="evil-oneshot",
            is_byok=True,
            skip_journal=True,
            raise_on_error=True,
        )
    assert j_exc.value.error_kind == "http_400"
    mock_create_custom.assert_not_called()
    mock_make_api_call.assert_not_called()
    assert net_events == []


@pytest.mark.asyncio
async def test_holder_oneshot_complete_with_custom_endpoint_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_team_sharing(monkeypatch, sharing="team", host_mode="oss")
    net_events = _install_network_tripwire(monkeypatch)

    holder_custom = {
        "name": "holder-oneshot",
        "model_id": "gpt-4o",
        "provider": "openai",
        "parameters": {"base_url": HOLDER_SAVED_BASE_URL},
    }
    _patch_resolve_llm_config_deps(
        monkeypatch,
        model_pref_by_user={HOLDER_ID: {"custom_models": [holder_custom]}},
        byok_configs={"openai": {"api_key": HOLDER_SECRET_KEY, "base_url": None}},
    )
    fake_client = create_autospec(llm_mod.BaseChatModel, instance=True)
    fake_client.metadata = {}
    fake_client.model_copy.return_value = fake_client
    mock_create_custom = create_autospec(llm_mod.create_llm_from_custom, return_value=fake_client)
    mock_make_api_call = create_autospec(
        api_call_mod.make_api_call,
        return_value=("ok-from-holder", {"input_tokens": 10, "output_tokens": 5}),
    )
    monkeypatch.setattr(llm_mod, "create_llm_from_custom", mock_create_custom)
    monkeypatch.setattr(service_mod, "make_api_call", mock_make_api_call)

    svc = service_mod.LLMService(agent_config=_minimal_agent_config())
    res = await complete_mod.dalton_complete(
        llm_service=svc,
        user_id=HOLDER_ID,
        user_prompt="ping",
        model="holder-oneshot",
        is_byok=True,
        skip_journal=True,
    )

    assert res.ok is True
    assert res.text == "ok-from-holder"
    assert mock_create_custom.call_count >= 1
    mock_make_api_call.assert_called_once()
    assert net_events == []


@pytest.mark.asyncio
async def test_everflow_subscription_oauth_unaffected_by_team_byok_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built-in OAuth subscription models keep resolving normally under team sharing."""
    from datetime import datetime, timedelta, timezone
    from src.server.services.oauth_protocol import TokenSet

    _configure_team_sharing(monkeypatch, sharing="team", host_mode="oss")
    net_events = _install_network_tripwire(monkeypatch)

    token = TokenSet(
        access_token="oauth-access-token",
        refresh_token="oauth-refresh-token",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        account_id="acct-123",
    )
    fake_oauth_client = create_autospec(llm_mod.BaseChatModel, instance=True)
    fake_oauth_client.metadata = {}
    monkeypatch.setattr(
        oauth_health_mod,
        "valid_token",
        create_autospec(oauth_health_mod.valid_token, return_value=token),
    )
    mock_create_llm = create_autospec(llm_mod.create_llm, return_value=fake_oauth_client)
    monkeypatch.setattr(llm_mod, "create_llm", mock_create_llm)
    monkeypatch.setattr(
        clients_mod,
        "require_policy",
        create_autospec(account_svc.require_policy, return_value=Policy()),
    )

    resolved = await clients_mod.resolve_model_client(
        MEMBER_ID,
        "gpt-6.1-terra-oauth",
        is_byok=True,
        _pref_cache={},
    )

    assert resolved.client is fake_oauth_client
    mock_create_llm.assert_called_once()
    assert net_events == []


# ---------------------------------------------------------------------------
# 5. Round 2 — Item 1: Custom provider target URL / SDK owned strictly by holder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_base_url", [None, "", HOLDER_SAVED_BASE_URL])
async def test_r2_non_manifest_custom_provider_missing_on_holder_returns_400(
    monkeypatch: pytest.MonkeyPatch,
    saved_base_url: str | None,
) -> None:
    """When holder has no custom_providers definition for a non-manifest slug, return 400 before building any client."""
    _configure_team_sharing(monkeypatch, sharing="team")
    net_events = _install_network_tripwire(monkeypatch)
    _patch_holder_prefs(monkeypatch, {"custom_providers": []})

    pref_cache = {
        "custom_providers": [{"name": "orphan-gw", "parent_provider": "deepseek"}],
        "custom_models": [
            {"name": "orphan-model", "model_id": "deepseek-chat", "provider": "orphan-gw"},
        ],
    }
    byok_cache = {
        "orphan-gw": {"api_key": HOLDER_SECRET_KEY, "base_url": saved_base_url},
    }
    mock_create_custom = create_autospec(llm_mod.create_llm_from_custom)
    mock_create_llm = create_autospec(llm_mod.create_llm)
    monkeypatch.setattr(llm_mod, "create_llm_from_custom", mock_create_custom)
    monkeypatch.setattr(llm_mod, "create_llm", mock_create_llm)

    # Via custom_models entry referencing non-manifest provider slug
    with pytest.raises(HTTPException) as exc_info:
        await clients_mod.resolve_byok_llm_client(
            MEMBER_ID,
            "orphan-model",
            is_byok=True,
            _pref_cache=pref_cache,
            _byok_cache=byok_cache,
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["type"] == "base_url_blocked"

    # Via direct model_name == non-manifest custom provider slug
    with pytest.raises(HTTPException) as exc_direct:
        await clients_mod.resolve_byok_llm_client(
            MEMBER_ID,
            "orphan-gw",
            is_byok=True,
            _pref_cache=pref_cache,
            _byok_cache=byok_cache,
        )
    assert exc_direct.value.status_code == 400
    assert exc_direct.value.detail["type"] == "base_url_blocked"

    mock_create_custom.assert_not_called()
    mock_create_llm.assert_not_called()
    assert net_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_base_url", [None, "", HOLDER_SAVED_BASE_URL])
async def test_r2_member_parent_provider_mismatch_with_holder_returns_400(
    monkeypatch: pytest.MonkeyPatch,
    saved_base_url: str | None,
) -> None:
    """When member's custom_providers parent_provider differs from holder's, return 400 and never build a client."""
    _configure_team_sharing(monkeypatch, sharing="team")
    net_events = _install_network_tripwire(monkeypatch)
    _patch_holder_prefs(
        monkeypatch,
        {"custom_providers": [{"name": "corp-gw", "parent_provider": "openai"}]},
    )

    pref_cache = {
        "custom_providers": [{"name": "corp-gw", "parent_provider": "deepseek"}],
        "custom_models": [
            {"name": "corp-model", "model_id": "deepseek-chat", "provider": "corp-gw"},
        ],
    }
    byok_cache = {
        "corp-gw": {"api_key": HOLDER_SECRET_KEY, "base_url": saved_base_url},
    }
    mock_create_custom = create_autospec(llm_mod.create_llm_from_custom)
    mock_create_llm = create_autospec(llm_mod.create_llm)
    monkeypatch.setattr(llm_mod, "create_llm_from_custom", mock_create_custom)
    monkeypatch.setattr(llm_mod, "create_llm", mock_create_llm)

    with pytest.raises(HTTPException) as exc_info:
        await clients_mod.resolve_byok_llm_client(
            MEMBER_ID,
            "corp-model",
            is_byok=True,
            _pref_cache=pref_cache,
            _byok_cache=byok_cache,
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["type"] == "base_url_blocked"
    mock_create_custom.assert_not_called()
    mock_create_llm.assert_not_called()
    assert net_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_base_url", [None, ""])
async def test_r2_member_uses_holder_custom_provider_parent_default_when_key_base_url_empty(
    monkeypatch: pytest.MonkeyPatch,
    saved_base_url: str | None,
) -> None:
    """With empty/None key base_url, member inherits holder's custom_provider parent SDK and default URL."""
    _configure_team_sharing(monkeypatch, sharing="team")
    net_events = _install_network_tripwire(monkeypatch)
    _patch_holder_prefs(
        monkeypatch,
        {"custom_providers": [{"name": "team-anthropic", "parent_provider": "anthropic"}]},
    )

    pref_cache = {
        "custom_models": [
            {"name": "team-claude", "model_id": "claude-sonnet-4-20250514", "provider": "team-anthropic"},
        ],
    }
    byok_cache = {
        "team-anthropic": {"api_key": HOLDER_SECRET_KEY, "base_url": saved_base_url},
    }
    sentinel_client = object()
    mock_create_custom = create_autospec(llm_mod.create_llm_from_custom, return_value=sentinel_client)
    monkeypatch.setattr(llm_mod, "create_llm_from_custom", mock_create_custom)

    client = await clients_mod.resolve_byok_llm_client(
        MEMBER_ID,
        "team-claude",
        is_byok=True,
        _pref_cache=pref_cache,
        _byok_cache=byok_cache,
    )
    assert client is sentinel_client
    mock_create_custom.assert_called_once()
    routed_entry = mock_create_custom.call_args.args[0]
    assert routed_entry["provider"] == "anthropic"
    expected_default = llm_mod.LLM.get_model_config().get_provider_info("anthropic").get("base_url")
    assert mock_create_custom.call_args.kwargs["base_url"] == expected_default
    assert mock_create_custom.call_args.kwargs["follow_redirects"] is False
    assert net_events == []


# ---------------------------------------------------------------------------
# 6. Round 2 — Item 2: Only holder may modify an existing shared key's base_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r2_only_holder_can_modify_existing_shared_key_base_url_in_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In both supabase and local auth modes, other admins get 403 when changing an existing shared key's base_url."""
    from contextlib import asynccontextmanager
    from psycopg import AsyncConnection, AsyncCursor
    import src.dalton.credentials as cred_mod
    import src.server.app.api_keys as api_keys_app
    import src.server.database.api_keys as api_keys_db
    from src.server.app.api_keys import UpdateApiKeysRequest

    _configure_team_sharing(monkeypatch, sharing="team", auth_mode="supabase")
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "test-byok-encryption-key-for-unit-tests")

    # 2a. Route-level base_urls-only and empty-key+base_urls requests rejected for SECOND_PM_ID
    fake_update_base_url = create_autospec(api_keys_app.update_base_url, return_value=True)
    fake_upsert = create_autospec(api_keys_app.upsert_api_key, return_value=None)
    monkeypatch.setattr(api_keys_app, "update_base_url", fake_update_base_url)
    monkeypatch.setattr(api_keys_app, "upsert_api_key", fake_upsert)

    for body in [
        UpdateApiKeysRequest(base_urls={"openai": ATTACKER_URL}),
        UpdateApiKeysRequest(api_keys={"openai": ""}, base_urls={"openai": ATTACKER_URL}),
    ]:
        with pytest.raises(HTTPException) as exc_403:
            await api_keys_app.update_api_keys(body=body, user_id=SECOND_PM_ID)
        assert exc_403.value.status_code == 403
        assert exc_403.value.detail == cred_mod.TEAM_CREDENTIAL_BASE_URL_FORBIDDEN_DETAIL

    fake_update_base_url.assert_not_awaited()
    fake_upsert.assert_not_awaited()

    # 2b. DB-level transactional check inside update_base_url & upsert_api_key (supabase mode)
    orig_get_db_conn = api_keys_db.get_db_connection
    executed: list[tuple[str, Any]] = []

    def _make_db(existing_row: dict[str, Any] | None):
        cur = create_autospec(AsyncCursor, instance=True)

        async def _exec(q: Any, p: Any = None, **kw: Any) -> Any:
            executed.append((str(q), p))
            return cur

        cur.execute.side_effect = _exec
        cur.fetchone.return_value = existing_row
        cur.rowcount = 1

        @asynccontextmanager
        async def _cursor_cm(*a: Any, **kw: Any):
            yield cur

        @asynccontextmanager
        async def _tx_cm(*a: Any, **kw: Any):
            yield None

        conn = create_autospec(AsyncConnection, instance=True)
        conn.cursor.side_effect = _cursor_cm
        conn.transaction.side_effect = _tx_cm

        @asynccontextmanager
        async def _get_conn(existing: Any = None):
            yield conn

        return create_autospec(orig_get_db_conn, side_effect=_get_conn)

    monkeypatch.setattr(
        api_keys_db,
        "get_db_connection",
        _make_db({"base_url": HOLDER_SAVED_BASE_URL}),
    )

    with pytest.raises(HTTPException) as exc_db_update:
        await api_keys_db.update_base_url(SECOND_PM_ID, "openai", ATTACKER_URL)
    assert exc_db_update.value.status_code == 403
    assert not any("UPDATE user_api_keys" in q for q, _ in executed)

    with pytest.raises(HTTPException) as exc_db_upsert:
        await api_keys_db.upsert_api_key(SECOND_PM_ID, "openai", "sk-new", base_url=ATTACKER_URL)
    assert exc_db_upsert.value.status_code == 403
    assert not any("INSERT INTO user_api_keys" in q for q, _ in executed)

    # Holder itself CAN update base_url and upsert with a new base_url
    executed.clear()
    updated = await api_keys_db.update_base_url(HOLDER_ID, "openai", HOLDER_SAVED_BASE_URL)
    assert updated is True
    assert any("UPDATE user_api_keys" in q for q, _ in executed)

    # 2c. DB-level transactional check in local auth mode (checks holder_user_id FOR SHARE inside tx)
    _configure_team_sharing(monkeypatch, sharing="team", auth_mode="local", pm_ids=None)
    local_sql: list[str] = []

    def _make_local_db():
        cur = create_autospec(AsyncCursor, instance=True)
        rows: list[dict[str, Any]] = []

        async def _exec(q: Any, p: Any = None, **kw: Any) -> Any:
            s = " ".join(str(q).split())
            local_sql.append(s)
            rows.clear()
            if "FROM dalton_credential_holder" in s:
                rows.append({"holder_user_id": HOLDER_ID})
            elif "FROM dalton_user_roles" in s:
                rows.append({"tier": "admin"})
            elif "SELECT base_url FROM user_api_keys" in s:
                rows.append({"base_url": HOLDER_SAVED_BASE_URL})
            return cur

        async def _fetchone() -> Any:
            return rows[0] if rows else None

        cur.execute.side_effect = _exec
        cur.fetchone.side_effect = _fetchone
        cur.rowcount = 1

        @asynccontextmanager
        async def _cursor_cm(*a: Any, **kw: Any):
            yield cur

        @asynccontextmanager
        async def _tx_cm(*a: Any, **kw: Any):
            yield None

        conn = create_autospec(AsyncConnection, instance=True)
        conn.cursor.side_effect = _cursor_cm
        conn.transaction.side_effect = _tx_cm

        @asynccontextmanager
        async def _get_conn(existing: Any = None):
            yield conn

        return create_autospec(orig_get_db_conn, side_effect=_get_conn)

    monkeypatch.setattr(api_keys_db, "get_db_connection", _make_local_db())
    with pytest.raises(HTTPException) as exc_local_403:
        await api_keys_db.update_base_url(SECOND_PM_ID, "openai", ATTACKER_URL)
    assert exc_local_403.value.status_code == 403
    assert any("FOR SHARE" in s for s in local_sql)
    assert not any("UPDATE user_api_keys" in s for s in local_sql)

    local_sql.clear()
    assert await api_keys_db.update_base_url(HOLDER_ID, "openai", HOLDER_SAVED_BASE_URL) is True
    assert any("UPDATE user_api_keys" in s for s in local_sql)


# ---------------------------------------------------------------------------
# 7. Round 2 — Item 3: Disable redirects on all SDKs & refuse routing headers
# ---------------------------------------------------------------------------


def test_r2_shared_key_clients_disable_redirects_for_openai_anthropic_gemini_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM(..., follow_redirects=False) disables redirects across OpenAI, Anthropic, Gemini, and OpenRouter."""
    net_events = _install_network_tripwire(monkeypatch)

    # OpenAI
    oai = llm_mod.create_llm_from_custom(
        {"name": "m-oai", "model_id": "gpt-4o", "provider": "openai"},
        api_key=HOLDER_SECRET_KEY,
        follow_redirects=False,
    )
    assert oai.root_client._client.follow_redirects is False
    assert oai.root_async_client._client.follow_redirects is False

    # Anthropic
    ant = llm_mod.create_llm_from_custom(
        {"name": "m-ant", "model_id": "claude-sonnet-4-20250514", "provider": "anthropic"},
        api_key=HOLDER_SECRET_KEY,
        follow_redirects=False,
    )
    assert ant._client._client.follow_redirects is False
    assert ant._async_client._client.follow_redirects is False

    # Gemini (both sync and async httpx clients, with aiohttp disabled)
    gem = llm_mod.create_llm_from_custom(
        {"name": "m-gem", "model_id": "gemini-2.5-flash", "provider": "gemini"},
        api_key=HOLDER_SECRET_KEY,
        follow_redirects=False,
    )
    api_cl = gem.client._api_client
    assert api_cl._use_aiohttp() is False
    assert api_cl._httpx_client.follow_redirects is False
    assert api_cl._async_httpx_client.follow_redirects is False

    # OpenRouter
    or_client = llm_mod.create_llm_from_custom(
        {"name": "m-or", "model_id": "openai/gpt-4o", "provider": "openrouter"},
        api_key=HOLDER_SECRET_KEY,
        follow_redirects=False,
    )
    assert or_client.follow_redirects is False

    # Muse native client (sdk="muse")
    muse_client = llm_mod.create_llm_from_custom(
        {"name": "m-muse", "model_id": "muse-spark-1.3", "provider": "muse-oauth"},
        api_key=HOLDER_SECRET_KEY,
        follow_redirects=False,
    )
    assert muse_client.root_client._client.follow_redirects is False
    assert muse_client.root_async_client._client.follow_redirects is False

    # Grok native client (sdk="grok")
    grok_client = llm_mod.create_llm_from_custom(
        {"name": "m-grok", "model_id": "grok-4", "provider": "xai-oauth"},
        api_key=HOLDER_SECRET_KEY,
        follow_redirects=False,
    )
    assert grok_client.root_client._client.follow_redirects is False
    assert grok_client.root_async_client._client.follow_redirects is False
    assert net_events == []


@pytest.mark.parametrize("client_line", ["httpx2", "httpx"])
def test_r3_gemini_no_redirect_transport_preserves_env_proxies(
    monkeypatch: pytest.MonkeyPatch,
    client_line: str,
) -> None:
    """Gemini follow_redirects=False keeps HTTP_PROXY/HTTPS_PROXY/NO_PROXY routing on sync and async clients."""
    import httpcore
    import httpcore2
    import httpx
    import httpx2

    net_events = _install_network_tripwire(monkeypatch)
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:8080")
    monkeypatch.setenv("NO_PROXY", "bypass.internal")
    if client_line == "httpx":
        monkeypatch.setenv("GOOGLE_GENAI_HTTP_CLIENT", "httpx")
    else:
        monkeypatch.delenv("GOOGLE_GENAI_HTTP_CLIENT", raising=False)

    gem = llm_mod.create_llm_from_custom(
        {"name": "m-gem-proxy", "model_id": "gemini-2.5-flash", "provider": "gemini"},
        api_key=HOLDER_SECRET_KEY,
        follow_redirects=False,
    )
    api_cl = gem.client._api_client
    assert api_cl._use_aiohttp() is False
    assert api_cl._httpx_client.follow_redirects is False
    assert api_cl._async_httpx_client.follow_redirects is False

    hx = httpx if client_line == "httpx" else httpx2
    hc = httpcore if client_line == "httpx" else httpcore2

    proxied_url = hx.URL("https://generativelanguage.googleapis.com/v1beta/models")
    bypass_url = hx.URL("https://bypass.internal/v1beta/models")

    sync_proxied = api_cl._httpx_client._transport_for_url(proxied_url)
    async_proxied = api_cl._async_httpx_client._transport_for_url(proxied_url)
    assert isinstance(sync_proxied._sync._pool, hc.HTTPProxy)
    assert isinstance(async_proxied._async._pool, hc.AsyncHTTPProxy)

    sync_bypass = api_cl._httpx_client._transport_for_url(bypass_url)
    async_bypass = api_cl._async_httpx_client._transport_for_url(bypass_url)
    assert sync_bypass is api_cl._httpx_client._transport
    assert async_bypass is api_cl._async_httpx_client._transport
    assert not isinstance(sync_bypass._sync._pool, hc.HTTPProxy)
    assert not isinstance(async_bypass._async._pool, hc.AsyncHTTPProxy)
    assert net_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model_id", "params"),
    [
        ("openai", "gpt-4o", {"default_headers": {"Host": "evil.example"}}),
        ("openai", "gpt-4o", {"default_headers": {"X-Forwarded-Host": "evil.example"}}),
        ("openai", "gpt-4o", {"default_headers": {":authority": "evil.example"}}),
        ("openai", "gpt-4o", {"default_headers": {"Forwarded": "host=evil.example"}}),
        ("openai", "gpt-4o", {"default_headers": {"X-Custom-Host": "evil.example"}}),
        ("openai", "gpt-4o", {"default_headers": {"X-Forward-To": "evil.example"}}),
        ("openai", "gpt-4o", {"additional_headers": {"Host": "evil.example"}}),
        ("openai", "gpt-4o", {"model_kwargs": {"default_headers": {"X-Forwarded-For": "1.2.3.4"}}}),
        ("openai", "gpt-4o", {"extra_headers": {"Host": "evil.example"}}),
        ("openai", "gpt-4o", {"extra_headers": {"X-Forwarded-Host": "evil.example"}}),
        ("openai", "gpt-4o", {"model_kwargs": {"extra_headers": {"Host": "evil.example"}}}),
        ("openai", "gpt-4o", {"model_kwargs": {"extra_headers": {":authority": "evil.example"}}}),
        ("anthropic", "claude-sonnet-4-20250514", {"extra_headers": {"Host": "evil.example"}}),
        ("anthropic", "claude-sonnet-4-20250514", {"extra_headers": {"Forwarded": "host=evil.example"}}),
        ("anthropic", "claude-sonnet-4-20250514", {"model_kwargs": {"extra_headers": {"X-Forward-To": "evil.example"}}}),
    ],
)
async def test_r2_member_routing_headers_rejected_at_runtime_and_save(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model_id: str,
    params: dict[str, Any],
) -> None:
    """Routing-altering headers in default_headers/additional_headers/extra_headers are rejected with 400."""
    _configure_team_sharing(monkeypatch, sharing="team", host_mode="oss")
    net_events = _install_network_tripwire(monkeypatch)
    _patch_holder_prefs(monkeypatch, {})

    entry = {
        "name": "hdr-exfil",
        "model_id": model_id,
        "provider": provider,
        "parameters": params,
    }
    mock_create_custom = create_autospec(llm_mod.create_llm_from_custom)
    mock_create_llm = create_autospec(llm_mod.create_llm)
    monkeypatch.setattr(llm_mod, "create_llm_from_custom", mock_create_custom)
    monkeypatch.setattr(llm_mod, "create_llm", mock_create_llm)

    # Runtime refusal
    with pytest.raises(HTTPException) as exc_rt:
        await clients_mod.resolve_byok_llm_client(
            MEMBER_ID,
            "hdr-exfil",
            is_byok=True,
            _pref_cache={"custom_models": [entry]},
            _byok_cache=_byok_cache_for(provider, {"api_key": HOLDER_SECRET_KEY, "base_url": None}),
        )
    assert exc_rt.value.status_code == 400
    assert exc_rt.value.detail["type"] == "base_url_blocked"
    mock_create_custom.assert_not_called()
    mock_create_llm.assert_not_called()

    # Save-time refusal
    with pytest.raises(HTTPException) as exc_save:
        await custom_catalog_mod.refuse_custom_model_endpoints([entry], user_id=MEMBER_ID)
    assert exc_save.value.status_code == 400
    assert net_events == []


@pytest.mark.asyncio
async def test_r3_non_routing_extra_headers_allowed_and_custom_provider_extra_headers_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Benign extra_headers stay allowed while routing extra_headers on custom_providers are refused."""
    _configure_team_sharing(monkeypatch, sharing="team", host_mode="oss")
    net_events = _install_network_tripwire(monkeypatch)

    benign = {
        "name": "hdr-benign",
        "model_id": "gpt-4o",
        "provider": "openai",
        "parameters": {
            "extra_headers": {"X-Trace-Id": "abc"},
            "model_kwargs": {"extra_headers": {"X-Request-Source": "unit-test"}},
        },
    }
    await custom_catalog_mod.refuse_custom_model_endpoints([benign], user_id=MEMBER_ID)

    bad_cp = {
        "name": "corp-gw",
        "parent_provider": "openai",
        "parameters": {"model_kwargs": {"extra_headers": {"X-Forwarded-Host": "evil.example"}}},
    }
    with pytest.raises(HTTPException) as exc_cp:
        custom_catalog_mod.validate_custom_providers([bad_cp], user_id=MEMBER_ID)
    assert exc_cp.value.status_code == 400
    assert "extra_headers.X-Forwarded-Host" in exc_cp.value.detail
    assert net_events == []


# ---------------------------------------------------------------------------
# 8. Round 2 — Item 4: Single-query local provider model discovery, fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r2_list_provider_models_single_read_and_no_fallback_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_provider_models reads base_url+api_key in one query and returns error without falling back to default URL."""
    import src.server.app.api_keys as api_keys_app
    from src.server.services.llm import provider_models as pm

    _configure_team_sharing(monkeypatch, sharing="team", host_mode="oss")
    net_events = _install_network_tripwire(monkeypatch)

    fake_local_models = create_autospec(
        pm.local_server_models,
        return_value=[{"id": "llama3", "name": "llama3"}],
    )
    monkeypatch.setattr(pm, "local_server_models", fake_local_models)

    # 4a. Single read returns both base_url and api_key from the same row
    orig_get_pair = api_keys_app.get_key_and_base_url_for_provider
    fake_get_pair = create_autospec(
        orig_get_pair,
        return_value={"api_key": HOLDER_SECRET_KEY, "base_url": HOLDER_SAVED_BASE_URL},
    )
    monkeypatch.setattr(api_keys_app, "get_key_and_base_url_for_provider", fake_get_pair)

    res = await api_keys_app.list_provider_models("ollama", user_id=MEMBER_ID)
    assert res == {"models": [{"id": "llama3", "name": "llama3"}]}
    fake_get_pair.assert_awaited_once_with(MEMBER_ID, "ollama")
    fake_local_models.assert_awaited_once_with(
        HOLDER_SAVED_BASE_URL, HOLDER_SECRET_KEY, transport=None
    )

    # 4b. Read error fails closed (503) for non-holder member and NEVER falls back to manifest default URL
    fake_local_models.reset_mock()
    fake_get_pair_err = create_autospec(
        orig_get_pair,
        side_effect=RuntimeError("db timeout"),
    )
    monkeypatch.setattr(api_keys_app, "get_key_and_base_url_for_provider", fake_get_pair_err)

    with pytest.raises(HTTPException) as exc_503:
        await api_keys_app.list_provider_models("ollama", user_id=MEMBER_ID)
    assert exc_503.value.status_code == 503
    fake_local_models.assert_not_awaited()

    # 4c. Holder preserves original best-effort fallback on DB/decrypt read errors
    orig_get_base = api_keys_app.get_base_url_for_provider
    monkeypatch.setattr(
        api_keys_app,
        "get_base_url_for_provider",
        create_autospec(orig_get_base, return_value=HOLDER_SAVED_BASE_URL),
    )
    res_holder_decrypt_err = await api_keys_app.list_provider_models("ollama", user_id=HOLDER_ID)
    assert res_holder_decrypt_err == {"models": [{"id": "llama3", "name": "llama3"}]}
    fake_local_models.assert_awaited_once_with(HOLDER_SAVED_BASE_URL, None, transport=None)

    fake_local_models.reset_mock()
    monkeypatch.setattr(
        api_keys_app,
        "get_base_url_for_provider",
        create_autospec(orig_get_base, side_effect=RuntimeError("db timeout")),
    )
    res_holder_db_err = await api_keys_app.list_provider_models("ollama", user_id=HOLDER_ID)
    assert res_holder_db_err == {"models": [{"id": "llama3", "name": "llama3"}]}
    assert fake_local_models.await_count == 1
    assert fake_local_models.await_args.args[1] is None

    # 4d. Sharing disabled also preserves original best-effort fallback on read errors
    fake_local_models.reset_mock()
    _configure_team_sharing(monkeypatch, sharing="off", host_mode="oss")
    res_personal_db_err = await api_keys_app.list_provider_models("ollama", user_id=MEMBER_ID)
    assert res_personal_db_err == {"models": [{"id": "llama3", "name": "llama3"}]}
    assert fake_local_models.await_count == 1
    assert fake_local_models.await_args.args[1] is None
    assert net_events == []


# ---------------------------------------------------------------------------
# 9. Round 2 — Item 5: Provider probe preserves 400 base_url_blocked & records failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r2_probe_provider_preserves_400_and_records_probe_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """probe_provider records probe_fail and propagates HTTP 400 base_url_blocked to the API layer."""
    import src.dalton.services.provider_status as provider_status_mod

    _configure_team_sharing(monkeypatch, sharing="team", host_mode="oss")
    net_events = _install_network_tripwire(monkeypatch)

    blocked_exc = HTTPException(
        status_code=400,
        detail={
            "message": "Model 'evil-model' uses a team-shared API key and cannot set custom endpoint.",
            "type": "base_url_blocked",
            "link": {"url": "/settings?tab=model", "label": "Open Settings"},
        },
    )
    mock_resolve = create_autospec(clients_mod.resolve_model_client, side_effect=blocked_exc)
    monkeypatch.setattr(clients_mod, "resolve_model_client", mock_resolve)

    recorded_events: list[dict[str, Any]] = []

    async def _record_event(**kwargs: Any) -> None:
        recorded_events.append(kwargs)

    monkeypatch.setattr(
        provider_status_mod,
        "record_provider_event",
        create_autospec(provider_status_mod.record_provider_event, side_effect=_record_event),
    )

    with pytest.raises(HTTPException) as exc_info:
        await provider_status_mod.probe_provider(
            "openai",
            user_id=MEMBER_ID,
            model="evil-model",
        )
    assert exc_info.value is blocked_exc
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["type"] == "base_url_blocked"
    assert len(recorded_events) == 1
    assert recorded_events[0]["kind"] == "probe_fail"
    assert recorded_events[0]["detail"]["error_kind"] == "base_url_blocked"
    assert recorded_events[0]["detail"]["status_code"] == 400
    assert net_events == []


# ---------------------------------------------------------------------------
# 10. Round 2 — Item 6: Save-time validation checks any new/modified entry, keeps verbatim legacy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r2_save_validation_rejects_modified_legacy_entry_but_keeps_verbatim_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing model_id/temperature on a legacy model with base_url is refused; submitting it verbatim is kept."""
    _configure_team_sharing(monkeypatch, sharing="team", host_mode="oss")
    net_events = _install_network_tripwire(monkeypatch)

    legacy = {
        "name": "legacy-bad",
        "model_id": "gpt-4o",
        "provider": "openai",
        "parameters": {"base_url": ATTACKER_URL, "temperature": 0.2},
    }
    clean_new = {
        "name": "clean-new",
        "model_id": "gpt-4o",
        "provider": "openai",
        "parameters": {"temperature": 0.3},
    }

    # Verbatim legacy entry alongside a new clean entry is allowed at save time
    await custom_catalog_mod.refuse_custom_model_endpoints(
        [legacy, clean_new],
        stored=[legacy],
        user_id=MEMBER_ID,
    )

    # Modifying model_id while keeping the same routing params is now rejected
    modified_model_id = {**legacy, "model_id": "gpt-4.1"}
    with pytest.raises(HTTPException) as exc_mid:
        await custom_catalog_mod.refuse_custom_model_endpoints(
            [modified_model_id],
            stored=[legacy],
            user_id=MEMBER_ID,
        )
    assert exc_mid.value.status_code == 400
    assert "parameters.base_url" in exc_mid.value.detail

    # Modifying temperature while keeping the same routing params is also rejected
    modified_temp = {**legacy, "parameters": {"base_url": ATTACKER_URL, "temperature": 0.9}}
    with pytest.raises(HTTPException) as exc_temp:
        await custom_catalog_mod.refuse_custom_model_endpoints(
            [modified_temp],
            stored=[legacy],
            user_id=MEMBER_ID,
        )
    assert exc_temp.value.status_code == 400
    assert "parameters.base_url" in exc_temp.value.detail
    assert net_events == []


# ---------------------------------------------------------------------------
# 11. Round 2/3 — Role & fallback rejections keep safe fallback and log refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r2_role_and_fallback_rejections_preserve_fallback_and_log_refusal(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When role or fallback models are blocked, safe fallback remains and server logs refusal type and model name."""
    import logging

    _configure_team_sharing(monkeypatch, sharing="team", host_mode="oss")
    net_events = _install_network_tripwire(monkeypatch)

    bad_role_model = {
        "name": "evil-fetch",
        "model_id": "gpt-4o",
        "provider": "openai",
        "parameters": {"base_url": ATTACKER_URL},
    }
    bad_fallback_model = {
        "name": "evil-fallback",
        "model_id": "gpt-4o",
        "provider": "openai",
        "parameters": {"default_headers": {"Host": "evil.example"}},
    }
    _patch_resolve_llm_config_deps(
        monkeypatch,
        model_pref_by_user={
            HOLDER_ID: {},
            MEMBER_ID: {
                "custom_models": [bad_role_model, bad_fallback_model],
                "fetch_model": "evil-fetch",
                "fallback_models": ["evil-fallback"],
            },
        },
        byok_configs={"openai": {"api_key": HOLDER_SECRET_KEY, "base_url": None}},
    )
    fake_builtin_client = create_autospec(llm_mod.BaseChatModel, instance=True)
    fake_builtin_client.metadata = {}
    fake_builtin_client.model_copy.return_value = fake_builtin_client
    mock_create_llm = create_autospec(llm_mod.create_llm, return_value=fake_builtin_client)
    mock_create_custom = create_autospec(llm_mod.create_llm_from_custom)
    monkeypatch.setattr(llm_mod, "create_llm", mock_create_llm)
    monkeypatch.setattr(llm_mod, "create_llm_from_custom", mock_create_custom)

    with caplog.at_level(logging.WARNING):
        resolved = await config_mod.resolve_llm_config(
            base_config=_minimal_agent_config(),
            user_id=MEMBER_ID,
            request_model="gpt-6.1-terra",
            is_byok=True,
            slot="primary",
            user_facing=False,
        )

    mock_create_custom.assert_not_called()
    assert resolved.llm_client is fake_builtin_client
    assert "evil-fallback" not in (resolved.fallback_llm_names or [])
    # Fetch role safely fell back to the main client
    assert resolved.subsidiary_llm_clients.get("fetch") is fake_builtin_client
    assert not hasattr(resolved, "model_resolution_notices")

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "base_url_blocked" in log_text
    assert "evil-fetch" in log_text
    assert "evil-fallback" in log_text
    assert net_events == []
