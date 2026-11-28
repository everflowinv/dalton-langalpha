"""Unit tests for Prod Audit 2 §2 (narrowed final scope): unattended pre-dispatch subscription fallback.

Covers:
1. Default-off switch & single Alembic head ``d061`` (``d062`` removed).
2. Trusted server-side unattended source classification (client spoofing rejected).
3. Structured cooldown & switch-trigger parity:
   - Terminal quota/auth refusals trigger; plain 429 (``rate_limited`` / unstructured) does NOT.
   - Transient ``OAuthRefreshUnavailable`` / ``oauth_refresh_unavailable`` stays retryable (does NOT switch).
   - Redis cooldown key parity with ``model_resilience.quota_redis_key`` (including ``credential_id``).
4. Unified admin-whitelisted ``$0`` subscription pool (``internal_llm.allowed_models``) shared across
   pre-dispatch automation/wakeup selection, ``/internal/llm/complete``, and ``assist_json``.
5. Pre-dispatch model selection through real call chains (with ``create_autospec`` doubles):
   - Primary subscription cooled down -> selects 2nd whitelisted pool model after ``policy_refuses``
     re-verification and persists selected route into run snapshot via real ``lifecycle.start_run``.
   - ``policy_refuses`` denies 2nd model -> skips to 3rd model; payer/subject mismatch rejected.
   - All pool models cooled down -> keeps existing failure behavior.
   - Plain 429 cooldown -> does NOT switch.
   - Switch OFF -> identical to base (pre-dispatch, normal retry ``verify_resume_route``,
     ``classify_error``, snapshot, ``/internal/llm/complete``, ``assist_json``).
6. ``/internal/llm/complete`` and ``assist_json`` (both explicit ``fallback_models`` and default chain):
   only switch when main switch is ON, only on structured terminal quota/auth triggers, strictly
   within the admin-whitelisted ``$0`` subscription pool with live credential verification.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from fastapi import HTTPException

from src.config.models import ModelsPolicy, OwnModelAccessPolicy, PolicyLayer
from src.dalton import settings as dalton_settings_mod
from src.dalton.app import internal_llm_router
from src.dalton.llm import assist as assist_mod
from src.dalton.llm import complete as complete_mod
from src.dalton.llm import oauth_refresh as oauth_refresh_mod
from src.dalton.llm import unattended_fallback as uf
from src.dalton.services import automation_subscription_fallback as auto_fb_mod
from src.dalton.services import automation_thread_model as auto_tm_mod
from src.dalton.services.team_model_catalog import snapshot as snap_mod
from src.dalton.services.team_model_catalog.constants import TURN_SNAPSHOT_SCHEMA
from src.dalton.services.team_model_catalog.snapshot import (
    RouteFingerprint,
    build_effective_catalog_snapshot,
    verify_resume_route,
)
from src.dalton.settings import is_unattended_quota_fallback_enabled
from src.llms.provider_errors import ProviderRefusal
from src.ptc_agent.agent.middleware.model_resilience import (
    _RemoteQuotaExhaustedError,
    quota_redis_key,
)
from src.server.database import oauth_tokens as oauth_tokens_db
from src.server.database.runs import lifecycle as lifecycle_db
from src.server.handlers.chat.error_handling import classify_error
from src.server.services import account as account_svc
from src.server.services.llm import config as llm_config_mod
from src.server.services.llm import user_models
from src.server.services.oauth_health import OAuthReauthRequired, OAuthRefreshUnavailable
from src.server.services.oauth_protocol import OAuthPlanNoApiAccess
from src.server.services.policy import ALLOW_ALL, Policy, merge as merge_policy

REPO_ROOT = Path(__file__).resolve().parents[3]


class _FakeModel:
    """Minimal chat-model double with call metadata for quota_redis_key parity."""

    def __init__(self, name: str, billing_type: str = "oauth", provider: str = "codex-oauth") -> None:
        self.model = name
        self._call_metadata = {
            "catalog_model": name,
            "manifest_model": name,
            "billing_type": billing_type,
            "pricing_provider": provider,
        }


def _make_dalton_config_with_pool(pool: list[str]) -> Any:
    return dalton_settings_mod.DaltonConfig(
        internal_llm=dalton_settings_mod.DaltonInternalLLMConfig(allowed_models=list(pool))
    )


def _make_fake_model_config(entries: dict[str, dict[str, Any]]) -> Any:
    class _MC:
        def get_model_config(self, key: str) -> dict[str, Any] | None:
            return entries.get(key)

        def get_provider_info(self, provider: str) -> dict[str, Any]:
            return {"provider": provider}

    return _MC()


# ---------------------------------------------------------------------------
# 1. Default-off switch & single Alembic head d061
# ---------------------------------------------------------------------------


def test_unattended_switch_defaults_false_and_single_head_d061(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", raising=False)
    assert is_unattended_quota_fallback_enabled() is False

    d062_path = (
        REPO_ROOT / "src" / "dalton" / "migrations" / "versions" / "d062_wakeup_quota_deferrals.py"
    )
    assert not d062_path.exists()

    alembic_cfg = AlembicConfig(str(REPO_ROOT / "src" / "dalton" / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "src" / "dalton" / "migrations"))
    script = ScriptDirectory.from_config(alembic_cfg)
    assert script.get_heads() == ["d061"]


# ---------------------------------------------------------------------------
# 2. Trusted server-side unattended source classification
# ---------------------------------------------------------------------------


def test_trusted_unattended_source_classification() -> None:
    # Interactive client request spoofing query_type="wakeup" must be rejected
    spoofed = SimpleNamespace(query_type="wakeup", platform=None, origin=None)
    assert uf.is_unattended_turn(spoofed, from_service=False) is False

    # Server-posted wakeup turn is trusted
    assert uf.is_unattended_turn(spoofed, from_service=True) is True

    # Automation firing metadata is trusted
    assert uf.is_unattended_turn(run_metadata={"automation_execution_id": "exec-1"}) is True
    assert uf.is_unattended_turn(SimpleNamespace(origin=SimpleNamespace(type="automation"))) is True

    # Internal host endpoint and assist calls are trusted
    assert uf.is_unattended_turn(internal_endpoint=True) is True
    assert uf.is_unattended_turn(assist_call=True) is True


# ---------------------------------------------------------------------------
# 3. Structured trigger & cooldown parity (plain 429 & OAuthRefreshUnavailable excluded)
# ---------------------------------------------------------------------------


def test_quota_or_auth_switch_trigger_rules() -> None:
    now = datetime.now(timezone.utc)
    plan_limit_exc = _RemoteQuotaExhaustedError(
        "Plan limit reached",
        429,
        expires_at=(now + timedelta(minutes=30)).timestamp(),
        provider_refusal=ProviderRefusal("plan_limit", until=now + timedelta(minutes=30)),
    )
    assert uf.is_quota_or_auth_switch_trigger(plan_limit_exc) is True
    assert uf.is_quota_or_auth_switch_trigger(OAuthReauthRequired("codex-oauth", "invalid_grant")) is True
    assert uf.is_quota_or_auth_switch_trigger(OAuthPlanNoApiAccess("codex-oauth")) is True
    assert uf.is_quota_or_auth_switch_trigger(
        HTTPException(status_code=400, detail={"type": "oauth_reauth_required"})
    ) is True

    # Plain 429 / rate_limited / unstructured bench MUST NOT trigger
    plain_429_refusal = _RemoteQuotaExhaustedError(
        "Rate limited",
        429,
        expires_at=(now + timedelta(minutes=10)).timestamp(),
        provider_refusal=ProviderRefusal("rate_limited", until=now + timedelta(minutes=10)),
    )
    unstructured_429 = _RemoteQuotaExhaustedError("Too many requests", 429, expires_at=(now + timedelta(minutes=5)).timestamp())
    assert uf.is_quota_or_auth_switch_trigger(plain_429_refusal) is False
    assert uf.is_quota_or_auth_switch_trigger(unstructured_429) is False
    assert uf.is_quota_or_auth_switch_trigger(RuntimeError("429 quota exceeded text")) is False

    # Transient OAuthRefreshUnavailable MUST remain retryable (Codex R3 #4)
    assert uf.is_quota_or_auth_switch_trigger(
        OAuthRefreshUnavailable("codex-oauth", "refresh_failed")
    ) is False
    assert uf.is_quota_or_auth_switch_trigger(
        HTTPException(status_code=503, detail={"type": "oauth_refresh_unavailable"})
    ) is False


@pytest.mark.asyncio
async def test_inspect_subscription_availability_structured_vs_plain_429_and_redis_key_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = "user-parity-1"
    model_name = "gpt-6.2-sol-oauth"
    provider = "codex-oauth"
    updated_at = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    conn_row = {
        "account_id": "acct-99",
        "email": "ops@everflow.test",
        "plan_type": "pro",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=2),
        "updated_at": updated_at,
        "needs_reauth_at": None,
        "last_error_code": None,
        "is_live": True,
    }

    get_conn_mock = create_autospec(
        oauth_tokens_db.get_oauth_connection, instance=False, return_value=conn_row
    )
    monkeypatch.setattr(oauth_tokens_db, "get_oauth_connection", get_conn_mock)
    monkeypatch.setattr(
        uf,
        "_resolve_model_subscription_info",
        lambda m, **_kw: (provider, provider, "gpt-6.2-sol", False),
    )

    # Verify exact Redis key parity with model_resilience.quota_redis_key
    fake_client = _FakeModel(model_name, billing_type="oauth", provider=provider)
    cred_id = uf.resolve_oauth_credential_id_from_row(conn_row)
    rebuild_spec = SimpleNamespace(
        user_id=user_id,
        provider=provider,
        model_name=model_name,
        credential_id=cred_id,
        custom_entry=None,
    )
    monkeypatch.setattr(oauth_refresh_mod, "oauth_rebuild_spec", lambda _c: rebuild_spec)
    mr_key = quota_redis_key(model_name, fake_client, user_id=user_id)

    now = datetime.now(timezone.utc)
    redis_store: dict[str, Any] = {}

    class _FakeRedis:
        async def get(self, key: str) -> Any:
            return redis_store.get(key)

    from src.utils.cache import redis_cache as redis_cache_mod

    monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: _FakeRedis())

    # 1) Structured plan_limit cooldown -> unavailable, exact key parity
    redis_store[mr_key] = {
        "expires_at": (now + timedelta(minutes=25)).timestamp(),
        "refusal": {"reason": "plan_limit", "until": (now + timedelta(minutes=25)).isoformat()},
    }
    avail = await uf.inspect_subscription_availability(user_id, model_name, now=now)
    assert avail.available is False
    assert avail.kind == "quota_cooldown"
    assert avail.refusal_code == "plan_limit"
    assert avail.cooldown_key == mr_key

    # 2) Plain 429 rate_limited -> available (no switch)
    redis_store[mr_key] = {
        "expires_at": (now + timedelta(minutes=10)).timestamp(),
        "refusal": {"reason": "rate_limited"},
    }
    avail_rl = await uf.inspect_subscription_availability(user_id, model_name, now=now)
    assert avail_rl.available is True

    # 3) Unstructured Redis 429 (missing refusal) -> available (no switch)
    redis_store[mr_key] = {
        "expires_at": (now + timedelta(minutes=10)).timestamp(),
        "status_code": 429,
        "message": "Too Many Requests",
    }
    avail_unstructured = await uf.inspect_subscription_availability(user_id, model_name, now=now)
    assert avail_unstructured.available is True


# ---------------------------------------------------------------------------
# 4. Unified admin whitelist $0 subscription pool (Codex R3 #2)
# ---------------------------------------------------------------------------


def test_resolve_unattended_fallback_chain_strictly_uses_admin_whitelist_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = [
        "gemini-4-hydrogen-high-oauth",
        "deepseek-flash",  # cash model must be filtered out even if listed
        "gemini-4-argon-medium-oauth",
        "gpt-6.2-sol-oauth",
    ]
    monkeypatch.setattr(dalton_settings_mod, "get_dalton_config", lambda: _make_dalton_config_with_pool(pool))

    fake_mc = _make_fake_model_config(
        {
            "gemini-4-hydrogen-high-oauth": {"provider": "antigravity-oauth", "model_id": "gemini-4-hydrogen-high"},
            "gemini-4-argon-medium-oauth": {"provider": "antigravity-oauth", "model_id": "gemini-4-argon-medium"},
            "gpt-6.2-sol-oauth": {"provider": "codex-oauth", "model_id": "gpt-6.2-sol"},
            "grok-4.7-oauth": {"provider": "xai-oauth", "model_id": "grok-4.7"},
            "deepseek-flash": {"provider": "deepseek", "model_id": "deepseek-chat"},
        }
    )
    from src.llms.llm import LLM as LLMFactory

    monkeypatch.setattr(LLMFactory, "get_model_config", classmethod(lambda cls: fake_mc))
    monkeypatch.setattr(
        complete_mod,
        "resolve_model_billing_and_provider",
        lambda m: ("oauth", "oauth-prov") if m.endswith("-oauth") else ("cash", "deepseek"),
    )

    chain = uf.resolve_unattended_fallback_chain(primary_model="gemini-4-hydrogen-high-oauth")
    assert chain == ["gemini-4-argon-medium-oauth", "gpt-6.2-sol-oauth"]
    assert "grok-4.7-oauth" not in chain
    assert "deepseek-flash" not in chain


# ---------------------------------------------------------------------------
# 5. Pre-dispatch model selection through REAL call chains + snapshot write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_dispatch_wakeup_real_chain_selects_second_pool_model_and_writes_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real chain: resolve_turn_config_with_automation_inheritance -> acquire_fallback_candidate_live
    (real policy_refuses) -> lifecycle.start_run writing effective_catalog_snapshot with selected model."""
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    user_id = "user-wake-pre"
    thread_id = str(uuid4())
    run_id = str(uuid4())
    workspace_id = str(uuid4())

    pool = [
        "gemini-4-hydrogen-high-oauth",
        "gemini-4-argon-medium-oauth",
        "gpt-6.2-sol-oauth",
    ]
    monkeypatch.setattr(dalton_settings_mod, "get_dalton_config", lambda: _make_dalton_config_with_pool(pool))

    catalog_entries = {
        "gemini-4-hydrogen-high-oauth": {"provider": "antigravity-oauth", "model_id": "gemini-4-hydrogen-high"},
        "gemini-4-argon-medium-oauth": {"provider": "codex-oauth", "model_id": "gemini-4-argon-medium"},
        "gpt-6.2-sol-oauth": {"provider": "codex-oauth", "model_id": "gpt-6.2-sol"},
    }
    fake_mc = _make_fake_model_config(catalog_entries)
    from src.llms.llm import LLM as LLMFactory

    monkeypatch.setattr(LLMFactory, "get_model_config", classmethod(lambda cls: fake_mc))
    monkeypatch.setattr(
        complete_mod,
        "resolve_model_billing_and_provider",
        lambda m: ("oauth", catalog_entries[m]["provider"]) if m in catalog_entries else ("cash", "deepseek"),
    )
    monkeypatch.setattr(
        uf,
        "_resolve_model_subscription_info",
        lambda m, **_kw: (
            (catalog_entries[m]["provider"], catalog_entries[m]["provider"], catalog_entries[m]["model_id"], False)
            if m in catalog_entries
            else (None, "", m, False)
        ),
    )

    now = datetime.now(timezone.utc)

    async def _fake_avail(uid: str, mk: str, **_kw: Any) -> uf.SubscriptionAvailability:
        if mk == "gemini-4-hydrogen-high-oauth":
            return uf.SubscriptionAvailability(
                available=False,
                kind="quota_cooldown",
                refusal_code="plan_limit",
                provider="antigravity-oauth",
                reset_at=now + timedelta(minutes=45),
                cooldown_key="quota:u:antigravity-oauth:vendor:google",
                model_name=mk,
            )
        return uf.SubscriptionAvailability(available=True, provider="codex-oauth", model_name=mk)

    avail_mock = create_autospec(uf.inspect_subscription_availability, instance=False, side_effect=_fake_avail)
    monkeypatch.setattr(uf, "inspect_subscription_availability", avail_mock)

    # Real Policy allowing all whitelisted models (re-verified via canonical policy_refuses)
    policy_mock = create_autospec(account_svc.require_policy, instance=False, return_value=ALLOW_ALL)
    monkeypatch.setattr(account_svc, "require_policy", policy_mock)

    prefs_mock = create_autospec(
        user_models.get_model_preference,
        instance=False,
        return_value={"preferred_model": "gemini-4-hydrogen-high-oauth"},
    )
    monkeypatch.setattr(user_models, "get_model_preference", prefs_mock)

    # Stub DB lookup in _resolve_automation_notification_context so no real PG is needed for wakeup lookup
    monkeypatch.setattr(
        auto_tm_mod,
        "_resolve_automation_notification_context",
        create_autospec(
            auto_tm_mod._resolve_automation_notification_context,
            instance=False,
            return_value=(None, None, {"preferred_model": "gemini-4-hydrogen-high-oauth"}, False, False, None),
        ),
    )

    async def _resolve_impl(
        model_name: str | None,
        *,
        reasoning_effort_override: str | None = None,
        ignore_custom_models_override: bool = False,
    ) -> Any:
        assert ignore_custom_models_override is True
        return SimpleNamespace(llm=SimpleNamespace(name=model_name, fallback=[]))

    resolve_mock = create_autospec(_resolve_impl, side_effect=_resolve_impl)
    fallback_records: list[dict[str, Any]] = []
    wakeup_req = SimpleNamespace(
        query_type="wakeup",
        platform=None,
        hitl_response=None,
        wakeup={"v": 1, "notices": [{"wakeup_id": "w-1"}]},
    )

    cfg = await auto_tm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=user_id,
        thread_id=thread_id,
        request=wakeup_req,
        requested_model="gemini-4-hydrogen-high-oauth",
        held_model=None,
        from_service=True,
        payer_user_id=user_id,
        resolve=resolve_mock,
        on_fallback=fallback_records.append,
    )

    # 1) Selected 2nd pool model & verified policy_refuses
    assert cfg.llm.name == "gemini-4-argon-medium-oauth"
    assert policy_mock.await_count >= 1
    assert len(fallback_records) == 1
    assert fallback_records[0]["original_model"] == "gemini-4-hydrogen-high-oauth"
    assert fallback_records[0]["fallback_model"] == "gemini-4-argon-medium-oauth"
    assert fallback_records[0]["selected_route"]["model_key"] == "gemini-4-argon-medium-oauth"
    assert fallback_records[0]["selected_route"]["billing"] == "subscription"

    # 2) Drive real lifecycle.start_run to verify selected model route is written into run snapshot
    captured_start_metadata: dict[str, Any] = {}

    class _FakeCursor:
        def __init__(self) -> None:
            self._row: Any = None

        async def __aenter__(self) -> "_FakeCursor":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def execute(self, sql: str, params: Any = None) -> None:
            sql_u = " ".join(sql.split()).upper()
            if "INSERT INTO CONVERSATION_RESPONSES" in sql_u:
                for p in params or ():
                    raw = getattr(p, "obj", p)
                    if isinstance(raw, dict) and "effective_catalog_snapshot" in raw:
                        captured_start_metadata.update(raw)
                self._row = {
                    "conversation_response_id": run_id,
                    "conversation_thread_id": thread_id,
                    "turn_index": 1,
                    "attempt_no": 1,
                    "status": "in_progress",
                    "metadata": dict(captured_start_metadata),
                    "created_at": now,
                }
            elif "FROM CONVERSATION_THREADS" in sql_u:
                self._row = {
                    "conversation_thread_id": thread_id,
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "current_status": "idle",
                    "active_run_id": None,
                }
            elif "AS NEXT_TURN" in sql_u:
                self._row = {"next_turn": 1}
            else:
                self._row = None

        async def fetchone(self) -> Any:
            return self._row

    class _FakeConn:
        @asynccontextmanager
        async def transaction(self):
            yield self

        def cursor(self, **_kw: Any) -> _FakeCursor:
            return _FakeCursor()

    @asynccontextmanager
    async def _fake_db_conn():
        yield _FakeConn()

    monkeypatch.setattr(lifecycle_db.pool, "get_db_connection", _fake_db_conn)
    # Build snapshot using fake catalog entries
    monkeypatch.setattr(
        snap_mod,
        "build_effective_catalog_snapshot",
        create_autospec(
            build_effective_catalog_snapshot,
            instance=False,
            side_effect=lambda cands, **_kw: {
                "schema": TURN_SNAPSHOT_SCHEMA,
                "catalog_revision": 7,
                "models": {
                    k: {
                        "model_key": k,
                        "provider": catalog_entries[k]["provider"],
                        "model_id": catalog_entries[k]["model_id"],
                        "billing": "subscription",
                    }
                    for k in cands
                    if k in catalog_entries
                },
                "variants": {},
            },
        ),
    )

    await lifecycle_db.start_run(
        run_id=run_id,
        thread_id=thread_id,
        user_id=user_id,
        request_key=str(uuid4()),
        metadata={
            "llm_model": cfg.llm.name,
            "subscription_fallback": fallback_records[0],
        },
        query=None,
    )

    assert captured_start_metadata["llm_model"] == "gemini-4-argon-medium-oauth"
    snap = captured_start_metadata["effective_catalog_snapshot"]
    assert snap["schema"] == TURN_SNAPSHOT_SCHEMA
    assert "gemini-4-argon-medium-oauth" in snap["models"]
    assert snap["models"]["gemini-4-argon-medium-oauth"]["billing"] == "subscription"
    assert captured_start_metadata["subscription_fallback"]["selected_route"]["model_key"] == (
        "gemini-4-argon-medium-oauth"
    )


@pytest.mark.asyncio
async def test_pre_dispatch_policy_refuses_second_model_and_picks_third(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When policy_refuses rejects the 2nd whitelisted model, pre-dispatch skips to the 3rd."""
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    user_id = "user-policy-skip"
    pool = [
        "gemini-4-hydrogen-high-oauth",
        "gemini-4-argon-medium-oauth",
        "gpt-6.2-sol-oauth",
    ]
    monkeypatch.setattr(dalton_settings_mod, "get_dalton_config", lambda: _make_dalton_config_with_pool(pool))

    catalog_entries = {
        "gemini-4-hydrogen-high-oauth": {"provider": "antigravity-oauth", "model_id": "gemini-4-hydrogen-high"},
        "gemini-4-argon-medium-oauth": {"provider": "antigravity-oauth", "model_id": "gemini-4-argon-medium"},
        "gpt-6.2-sol-oauth": {"provider": "codex-oauth", "model_id": "gpt-6.2-sol"},
    }
    fake_mc = _make_fake_model_config(catalog_entries)
    from src.llms.llm import LLM as LLMFactory

    monkeypatch.setattr(LLMFactory, "get_model_config", classmethod(lambda cls: fake_mc))
    monkeypatch.setattr(
        complete_mod,
        "resolve_model_billing_and_provider",
        lambda m: ("oauth", catalog_entries[m]["provider"]),
    )
    monkeypatch.setattr(
        uf,
        "_resolve_model_subscription_info",
        lambda m, **_kw: (
            catalog_entries[m]["provider"],
            catalog_entries[m]["provider"],
            catalog_entries[m]["model_id"],
            False,
        ),
    )

    now = datetime.now(timezone.utc)

    async def _avail(uid: str, mk: str, **_kw: Any) -> uf.SubscriptionAvailability:
        if mk == "gemini-4-hydrogen-high-oauth":
            return uf.SubscriptionAvailability(
                available=False,
                kind="quota_cooldown",
                refusal_code="plan_limit",
                provider="antigravity-oauth",
                reset_at=now + timedelta(minutes=30),
                cooldown_key="quota:u:antigravity-oauth:model:hydrogen",
                model_name=mk,
            )
        return uf.SubscriptionAvailability(available=True, provider=catalog_entries[mk]["provider"], model_name=mk)

    monkeypatch.setattr(uf, "inspect_subscription_availability", create_autospec(uf.inspect_subscription_availability, instance=False, side_effect=_avail))

    # Org policy denies gemini-4-argon-medium-oauth and allows gpt-6.2-sol-oauth
    restrictive_policy: Policy = merge_policy(
        PolicyLayer(),
        PolicyLayer(
            models=ModelsPolicy(allowed=["gemini-4-hydrogen-high-oauth", "gpt-6.2-sol-oauth"]),
            own_model_access=OwnModelAccessPolicy(subscriptions=True),
        ),
    )
    monkeypatch.setattr(
        account_svc,
        "require_policy",
        create_autospec(account_svc.require_policy, instance=False, return_value=restrictive_policy),
    )
    monkeypatch.setattr(
        user_models,
        "get_model_preference",
        create_autospec(user_models.get_model_preference, instance=False, return_value={}),
    )

    async def _fake_resolve_llm(*args: Any, **kwargs: Any) -> Any:
        chosen = args[2]
        return SimpleNamespace(llm=SimpleNamespace(name=chosen, fallback=[]))

    resolve_llm_mock = create_autospec(
        llm_config_mod.resolve_llm_config, instance=False, side_effect=_fake_resolve_llm
    )
    monkeypatch.setattr(auto_fb_mod, "resolve_llm_config", resolve_llm_mock)

    req = SimpleNamespace(
        llm_model="gemini-4-hydrogen-high-oauth",
        llm_model_system_only=False,
        reasoning_effort=None,
        fast_mode=None,
        subagents_enabled=None,
        origin=SimpleNamespace(type="automation"),
    )
    cfg, run_meta = await auto_fb_mod.resolve_ptc_turn_llm_config(
        SimpleNamespace(),
        user_id,
        req,
        identity=SimpleNamespace(recorded_as=user_id),
        run_metadata={"automation_execution_id": "exec-policy"},
    )
    assert cfg.llm.name == "gpt-6.2-sol-oauth"
    assert run_meta is not None
    assert run_meta["subscription_fallback"]["fallback_model"] == "gpt-6.2-sol-oauth"

    # Payer/subject mismatch must be rejected by live admission
    mismatch = await uf.acquire_fallback_candidate_live(
        subject_user_id=user_id,
        payer_user_id="other-payer",
        candidate_model="gpt-6.2-sol-oauth",
    )
    assert mismatch.admitted is False
    assert mismatch.reason == "payer_subject_mismatch"


@pytest.mark.asyncio
async def test_pre_dispatch_all_cooled_down_plain_429_refresh_unavailable_and_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers: all cooled down -> fails as before; plain 429 -> no switch;
    OAuthRefreshUnavailable -> stays retryable; switch OFF -> unchanged."""
    user_id = "user-edge-cases"
    pool = ["gemini-4-hydrogen-high-oauth", "gemini-4-argon-medium-oauth"]
    monkeypatch.setattr(dalton_settings_mod, "get_dalton_config", lambda: _make_dalton_config_with_pool(pool))

    catalog_entries = {
        "gemini-4-hydrogen-high-oauth": {"provider": "antigravity-oauth", "model_id": "gemini-4-hydrogen-high"},
        "gemini-4-argon-medium-oauth": {"provider": "codex-oauth", "model_id": "gemini-4-argon-medium"},
    }
    fake_mc = _make_fake_model_config(catalog_entries)
    from src.llms.llm import LLM as LLMFactory

    monkeypatch.setattr(LLMFactory, "get_model_config", classmethod(lambda cls: fake_mc))
    monkeypatch.setattr(
        complete_mod,
        "resolve_model_billing_and_provider",
        lambda m: ("oauth", catalog_entries[m]["provider"]),
    )
    monkeypatch.setattr(
        uf,
        "_resolve_model_subscription_info",
        lambda m, **_kw: (
            catalog_entries[m]["provider"],
            catalog_entries[m]["provider"],
            catalog_entries[m]["model_id"],
            False,
        ),
    )
    orig_require_policy = account_svc.require_policy
    monkeypatch.setattr(
        account_svc,
        "require_policy",
        create_autospec(orig_require_policy, instance=False, return_value=ALLOW_ALL),
    )
    monkeypatch.setattr(
        user_models,
        "get_model_preference",
        create_autospec(user_models.get_model_preference, instance=False, return_value={}),
    )

    now = datetime.now(timezone.utc)
    orig_inspect = uf.inspect_subscription_availability

    # Case A: All pool models cooled down -> keeps existing behavior (resolves primary and fails as before)
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    all_cooled = create_autospec(
        orig_inspect,
        instance=False,
        side_effect=lambda uid, mk, **_kw: uf.SubscriptionAvailability(
            available=False,
            kind="quota_cooldown",
            refusal_code="plan_limit",
            provider=catalog_entries[mk]["provider"],
            reset_at=now + timedelta(minutes=30),
            model_name=mk,
        ),
    )
    monkeypatch.setattr(uf, "inspect_subscription_availability", all_cooled)

    quota_err = _RemoteQuotaExhaustedError(
        "Plan limit reached",
        429,
        expires_at=(now + timedelta(minutes=30)).timestamp(),
        provider_refusal=ProviderRefusal("plan_limit", until=now + timedelta(minutes=30)),
    )
    monkeypatch.setattr(
        auto_fb_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, instance=False, side_effect=quota_err),
    )
    with pytest.raises(_RemoteQuotaExhaustedError):
        await auto_fb_mod.resolve_automation_turn_llm_config(
            SimpleNamespace(), user_id, "gemini-4-hydrogen-high-oauth"
        )

    # Case B: Plain 429 (available=True because inspect_subscription_availability ignores rate_limited) -> no switch
    avail_plain_429 = create_autospec(
        orig_inspect,
        instance=False,
        side_effect=lambda uid, mk, **_kw: uf.SubscriptionAvailability(available=True, model_name=mk),
    )
    monkeypatch.setattr(uf, "inspect_subscription_availability", avail_plain_429)
    resolve_ok = create_autospec(
        llm_config_mod.resolve_llm_config,
        instance=False,
        side_effect=lambda *args, **_kw: SimpleNamespace(llm=SimpleNamespace(name=args[2])),
    )
    monkeypatch.setattr(auto_fb_mod, "resolve_llm_config", resolve_ok)
    cfg_plain, rec_plain = await auto_fb_mod.resolve_automation_turn_llm_config(
        SimpleNamespace(), user_id, "gemini-4-hydrogen-high-oauth"
    )
    assert cfg_plain.llm.name == "gemini-4-hydrogen-high-oauth"
    assert rec_plain is None

    # Case C: Transient oauth_refresh_unavailable (503) stays retryable when switch is ON (Codex R3 #4)
    refresh_503 = HTTPException(
        status_code=503,
        detail={"type": "oauth_refresh_unavailable", "provider": "antigravity-oauth"},
    )
    monkeypatch.setattr(
        auto_fb_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, instance=False, side_effect=refresh_503),
    )
    with pytest.raises(HTTPException) as exc_info:
        await auto_fb_mod.resolve_automation_turn_llm_config(
            SimpleNamespace(), user_id, "gemini-4-hydrogen-high-oauth"
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["type"] == "oauth_refresh_unavailable"

    # Case D: Switch OFF -> never probes cooldown and keeps primary unchanged
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "0")
    probe_should_not_run = create_autospec(
        orig_inspect,
        instance=False,
        side_effect=AssertionError("should not probe when switch is OFF"),
    )
    monkeypatch.setattr(uf, "inspect_subscription_availability", probe_should_not_run)
    monkeypatch.setattr(auto_fb_mod, "resolve_llm_config", resolve_ok)
    cfg_off, rec_off = await auto_fb_mod.resolve_automation_turn_llm_config(
        SimpleNamespace(), user_id, "gemini-4-hydrogen-high-oauth"
    )
    assert cfg_off.llm.name == "gemini-4-hydrogen-high-oauth"
    assert rec_off is None


# ---------------------------------------------------------------------------
# 6. Switch-OFF contrast tests (normal retry, error classification, snapshot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_preserves_retry_verify_resume_route_error_classification_and_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "0")

    # 1) classify_error keeps 429 rate/quota error recoverable as in base
    now = datetime.now(timezone.utc)
    quota_exc = _RemoteQuotaExhaustedError(
        "Rate limit 429 reached",
        429,
        expires_at=(now + timedelta(minutes=30)).timestamp(),
        provider_refusal=ProviderRefusal("plan_limit", until=now + timedelta(minutes=30)),
    )
    cls_res = classify_error(quota_exc)
    assert cls_res["is_recoverable"] is True

    # 2) verify_resume_route verifies primary run model cleanly without active_fallback_route drift
    run_row = {
        "conversation_response_id": str(uuid4()),
        "metadata": {
            "llm_model": "gemini-4-hydrogen-high-oauth",
            "effective_catalog_snapshot": {
                "schema": TURN_SNAPSHOT_SCHEMA,
                "catalog_revision": 3,
                "models": {
                    "gemini-4-hydrogen-high-oauth": {
                        "model_key": "gemini-4-hydrogen-high-oauth",
                        "provider": "antigravity-oauth",
                        "model_id": "gemini-4-hydrogen-high",
                        "billing": "subscription",
                    }
                },
                "variants": {},
            },
        },
    }
    await verify_resume_route(
        run_row["conversation_response_id"],
        RouteFingerprint(
            model_key="gemini-4-hydrogen-high-oauth",
            provider="antigravity-oauth",
            model_id="gemini-4-hydrogen-high",
            billing="subscription",
        ),
        run_row=run_row,
    )

    # Mismatched model on normal retry still raises 400 catalog_route_changed
    with pytest.raises(HTTPException) as mismatch_exc:
        await verify_resume_route(
            run_row["conversation_response_id"],
            RouteFingerprint(
                model_key="gpt-6.2-sol-oauth",
                provider="codex-oauth",
                model_id="gpt-6.2-sol",
                billing="subscription",
            ),
            run_row=run_row,
        )
    assert mismatch_exc.value.status_code == 400
    assert mismatch_exc.value.detail["code"] == "catalog_route_changed"


# ---------------------------------------------------------------------------
# 7. Host /internal/llm/complete (switch OFF vs ON, structured triggers & whitelist)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internal_llm_complete_switch_off_and_on_whitelist_structured_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = [
        "gemini-4-hydrogen-high-oauth",
        "gemini-4-argon-medium-oauth",
        "gpt-6.2-sol-oauth",
    ]
    monkeypatch.setattr(dalton_settings_mod, "get_dalton_config", lambda: _make_dalton_config_with_pool(pool))
    monkeypatch.setattr(internal_llm_router, "get_allowed_host_oauth_models", lambda: frozenset(pool))

    catalog_entries = {
        "gemini-4-hydrogen-high-oauth": {"provider": "antigravity-oauth", "model_id": "gemini-4-hydrogen-high"},
        "gemini-4-argon-medium-oauth": {"provider": "codex-oauth", "model_id": "gemini-4-argon-medium"},
        "gpt-6.2-sol-oauth": {"provider": "codex-oauth", "model_id": "gpt-6.2-sol"},
    }
    fake_mc = _make_fake_model_config(catalog_entries)
    from src.llms.llm import LLM as LLMFactory

    monkeypatch.setattr(LLMFactory, "get_model_config", classmethod(lambda cls: fake_mc))
    monkeypatch.setattr(
        complete_mod,
        "resolve_model_billing_and_provider",
        lambda m: ("oauth", catalog_entries[m]["provider"]) if m in catalog_entries else ("cash", "deepseek"),
    )
    monkeypatch.setattr(
        uf,
        "_resolve_model_subscription_info",
        lambda m, **_kw: (
            (catalog_entries[m]["provider"], catalog_entries[m]["provider"], catalog_entries[m]["model_id"], False)
            if m in catalog_entries
            else (None, "", m, False)
        ),
    )

    from src.dalton import access_tiers

    monkeypatch.setattr(
        access_tiers,
        "get_primary_admin_user_id",
        create_autospec(access_tiers.get_primary_admin_user_id, instance=False, return_value="admin-1"),
    )

    req = internal_llm_router.InternalLLMCompleteRequest(
        model="gemini-4-hydrogen-high-oauth",
        user_prompt="ping",
        purpose="unit_test",
    )

    # A) Switch OFF: no ignore_custom_models, no fallback even on structured plan_limit
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "0")
    off_calls: list[dict[str, Any]] = []

    async def _complete_off(**kwargs: Any) -> complete_mod.DaltonCompleteResult:
        off_calls.append(kwargs)
        return complete_mod.DaltonCompleteResult(
            ok=False,
            status="failed",
            model=kwargs["model"],
            error="plan limit",
            refusal_reason="plan_limit",
            quota_or_auth_trigger=True,
        )

    monkeypatch.setattr(
        internal_llm_router,
        "dalton_complete",
        create_autospec(complete_mod.dalton_complete, instance=False, side_effect=_complete_off),
    )
    resp_off = await internal_llm_router.internal_llm_complete(req)
    assert resp_off.ok is False
    assert len(off_calls) == 1
    assert "ignore_custom_models" not in off_calls[0]

    # B) Switch ON + plain 429 / free-form "billing" text -> NO fallback
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    monkeypatch.setattr(
        uf,
        "inspect_subscription_availability",
        create_autospec(
            uf.inspect_subscription_availability,
            instance=False,
            side_effect=lambda uid, mk, **_kw: uf.SubscriptionAvailability(available=True, model_name=mk),
        ),
    )
    plain_calls: list[dict[str, Any]] = []

    async def _complete_plain_429(**kwargs: Any) -> complete_mod.DaltonCompleteResult:
        plain_calls.append(kwargs)
        return complete_mod.DaltonCompleteResult(
            ok=False,
            status="indeterminate",
            model=kwargs["model"],
            error_kind="http_429",
            error="HTTP 429 rate_limit billing quota text",
            refusal_reason="rate_limited",
            quota_or_auth_trigger=False,
        )

    monkeypatch.setattr(
        internal_llm_router,
        "dalton_complete",
        create_autospec(complete_mod.dalton_complete, instance=False, side_effect=_complete_plain_429),
    )
    resp_plain = await internal_llm_router.internal_llm_complete(req)
    assert resp_plain.ok is False
    assert len(plain_calls) == 1
    assert plain_calls[0]["ignore_custom_models"] is True

    # C) Switch ON + structured plan_limit -> falls back in admin whitelist order with ignore_custom_models=True
    on_calls: list[dict[str, Any]] = []

    async def _complete_on(**kwargs: Any) -> complete_mod.DaltonCompleteResult:
        on_calls.append(kwargs)
        if kwargs["model"] == "gemini-4-hydrogen-high-oauth":
            return complete_mod.DaltonCompleteResult(
                ok=False,
                status="failed",
                model=kwargs["model"],
                refusal_reason="plan_limit",
                quota_or_auth_trigger=True,
                error="plan_limit",
            )
        return complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            model=kwargs["model"],
            text="ok-from-second",
        )

    monkeypatch.setattr(
        internal_llm_router,
        "dalton_complete",
        create_autospec(complete_mod.dalton_complete, instance=False, side_effect=_complete_on),
    )
    resp_on = await internal_llm_router.internal_llm_complete(req)
    assert resp_on.ok is True
    assert resp_on.model == "gemini-4-argon-medium-oauth"
    assert [c["model"] for c in on_calls] == [
        "gemini-4-hydrogen-high-oauth",
        "gemini-4-argon-medium-oauth",
    ]
    assert all(c["ignore_custom_models"] is True for c in on_calls)


# ---------------------------------------------------------------------------
# 8. assist_json (explicit fallback_models & default chain; Codex R3 #1 & #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assist_json_explicit_and_default_fallbacks_enforced_when_on_and_unchanged_when_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = [
        "gemini-4-hydrogen-high-oauth",
        "gemini-4-argon-medium-oauth",
        "gpt-6.2-sol-oauth",
    ]
    monkeypatch.setattr(dalton_settings_mod, "get_dalton_config", lambda: _make_dalton_config_with_pool(pool))

    catalog_entries = {
        "gemini-4-hydrogen-high-oauth": {"provider": "antigravity-oauth", "model_id": "gemini-4-hydrogen-high"},
        "gemini-4-argon-medium-oauth": {"provider": "codex-oauth", "model_id": "gemini-4-argon-medium"},
        "gpt-6.2-sol-oauth": {"provider": "codex-oauth", "model_id": "gpt-6.2-sol"},
        "grok-4.7-oauth": {"provider": "xai-oauth", "model_id": "grok-4.7"},
        "deepseek-flash": {"provider": "deepseek", "model_id": "deepseek-chat"},
    }
    fake_mc = _make_fake_model_config(catalog_entries)
    from src.llms.llm import LLM as LLMFactory

    monkeypatch.setattr(LLMFactory, "get_model_config", classmethod(lambda cls: fake_mc))
    monkeypatch.setattr(
        complete_mod,
        "resolve_model_billing_and_provider",
        lambda m: ("oauth", catalog_entries[m]["provider"]) if m.endswith("-oauth") else ("cash", "deepseek"),
    )
    monkeypatch.setattr(
        uf,
        "_resolve_model_subscription_info",
        lambda m, **_kw: (
            (catalog_entries[m]["provider"], catalog_entries[m]["provider"], catalog_entries[m]["model_id"], False)
            if m.endswith("-oauth")
            else (None, "deepseek", m, False)
        ),
    )
    monkeypatch.setattr(
        assist_mod,
        "resolve_assist_model",
        create_autospec(
            assist_mod.resolve_assist_model,
            instance=False,
            side_effect=lambda role="assist", explicit_model=None, **_kw: explicit_model or "gemini-4-hydrogen-high-oauth",
        ),
    )

    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}
    orig_inspect = uf.inspect_subscription_availability

    # A) Switch ON + explicit fallback_models containing cash & non-whitelisted OAuth:
    #    plain 429 MUST NOT fall back (Codex R3 #1)
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    monkeypatch.setattr(
        uf,
        "inspect_subscription_availability",
        create_autospec(
            orig_inspect,
            instance=False,
            side_effect=lambda uid, mk, **_kw: uf.SubscriptionAvailability(available=True, model_name=mk),
        ),
    )

    calls_plain: list[dict[str, Any]] = []

    async def _assist_plain_429(**kwargs: Any) -> complete_mod.DaltonCompleteResult:
        calls_plain.append(kwargs)
        return complete_mod.DaltonCompleteResult(
            ok=False,
            status="indeterminate",
            model=kwargs["request_model"],
            error_kind="http_429",
            error="429 rate limited",
            refusal_reason="rate_limited",
            quota_or_auth_trigger=False,
        )

    monkeypatch.setattr(
        assist_mod,
        "dalton_complete",
        create_autospec(complete_mod.dalton_complete, instance=False, side_effect=_assist_plain_429),
    )
    res_plain = await assist_mod.assist_json(
        task="audit.judge",
        instructions="judge",
        payload={"x": 1},
        schema=schema,
        user_id="u-1",
        model="gemini-4-hydrogen-high-oauth",
        fallback_models=["deepseek-flash", "grok-4.7-oauth", "gemini-4-argon-medium-oauth"],
    )
    assert res_plain.ok is False
    assert [c["request_model"] for c in calls_plain] == ["gemini-4-hydrogen-high-oauth"]

    # B) Switch ON + explicit fallback_models + structured plan_limit:
    #    skips deepseek-flash (cash), skips grok-4.7-oauth (not in whitelist), skips disconnected argon,
    #    switches ONLY to whitelisted available gpt-6.2-sol-oauth with ignore_custom_models=True
    async def _avail_skip_argon(uid: str, mk: str, **_kw: Any) -> uf.SubscriptionAvailability:
        if mk == "gemini-4-argon-medium-oauth":
            return uf.SubscriptionAvailability(
                available=False, kind="auth_unavailable", refusal_code="oauth_required", model_name=mk
            )
        return uf.SubscriptionAvailability(available=True, model_name=mk)

    monkeypatch.setattr(
        uf,
        "inspect_subscription_availability",
        create_autospec(orig_inspect, instance=False, side_effect=_avail_skip_argon),
    )

    calls_structured: list[dict[str, Any]] = []

    async def _assist_structured(**kwargs: Any) -> complete_mod.DaltonCompleteResult:
        calls_structured.append(kwargs)
        if kwargs["request_model"] == "gemini-4-hydrogen-high-oauth":
            return complete_mod.DaltonCompleteResult(
                ok=False,
                status="failed",
                model=kwargs["request_model"],
                error="plan limit reached",
                refusal_reason="plan_limit",
                quota_or_auth_trigger=True,
            )
        return complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            model=kwargs["request_model"],
            data={"verdict": "supported"},
            text='{"verdict": "supported"}',
        )

    monkeypatch.setattr(
        assist_mod,
        "dalton_complete",
        create_autospec(complete_mod.dalton_complete, instance=False, side_effect=_assist_structured),
    )
    res_struct = await assist_mod.assist_json(
        task="audit.judge",
        instructions="judge",
        payload={"x": 1},
        schema=schema,
        user_id="u-1",
        model="gemini-4-hydrogen-high-oauth",
        fallback_models=[
            "deepseek-flash",
            "grok-4.7-oauth",
            "gemini-4-argon-medium-oauth",
            "gpt-6.2-sol-oauth",
        ],
    )
    assert res_struct.ok is True
    assert res_struct.model == "gpt-6.2-sol-oauth"
    assert [c["request_model"] for c in calls_structured] == [
        "gemini-4-hydrogen-high-oauth",
        "gpt-6.2-sol-oauth",
    ]
    assert calls_structured[1]["ignore_custom_models"] is True

    # C) Switch OFF contrast: explicit own_chain preserves base behavior (429 falls back to deepseek-flash)
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "0")
    calls_off: list[dict[str, Any]] = []

    async def _assist_off(**kwargs: Any) -> complete_mod.DaltonCompleteResult:
        calls_off.append(kwargs)
        if kwargs["request_model"] == "gemini-4-hydrogen-high-oauth":
            return complete_mod.DaltonCompleteResult(
                ok=False,
                status="indeterminate",
                model=kwargs["request_model"],
                error_kind="http_429",
                error="429 rate limited",
            )
        return complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            model=kwargs["request_model"],
            data={"verdict": "supported"},
            text='{"verdict": "supported"}',
        )

    monkeypatch.setattr(
        assist_mod,
        "dalton_complete",
        create_autospec(complete_mod.dalton_complete, instance=False, side_effect=_assist_off),
    )
    res_off = await assist_mod.assist_json(
        task="audit.judge",
        instructions="judge",
        payload={"x": 1},
        schema=schema,
        user_id="u-1",
        model="gemini-4-hydrogen-high-oauth",
        fallback_models=["deepseek-flash"],
    )
    assert res_off.ok is True
    assert res_off.model == "deepseek-flash"
    assert "ignore_custom_models" not in calls_off[1]


# ---------------------------------------------------------------------------
# 9. Round 5 fixes: candidate cooldown, effort re-verify, assist repair,
#    transient OAuth refresh, internal_llm journal dedup, wakeup default fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limited_candidate_skipped_for_next_available_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opus exhausted (plan_limit), Gemini in plain rate_limited cooldown, Codex available -> selects Codex.

    Plain rate_limited still does NOT trigger switching when it is the primary model,
    but DOES mark a fallback candidate unavailable during candidate selection.
    """
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    user_id = "user-r5-rl"
    pool = [
        "claude-opus-5-5-oauth",
        "gemini-4-hydrogen-high-oauth",
        "gpt-6.2-sol-oauth",
    ]
    monkeypatch.setattr(
        dalton_settings_mod, "get_dalton_config", lambda: _make_dalton_config_with_pool(pool)
    )

    catalog_entries = {
        "claude-opus-5-5-oauth": {"provider": "claude-oauth", "model_id": "claude-opus-5-5"},
        "gemini-4-hydrogen-high-oauth": {
            "provider": "antigravity-oauth",
            "model_id": "gemini-4-hydrogen-high",
        },
        "gpt-6.2-sol-oauth": {"provider": "codex-oauth", "model_id": "gpt-6.2-sol"},
    }
    fake_mc = _make_fake_model_config(catalog_entries)
    from src.llms.llm import LLM as LLMFactory

    monkeypatch.setattr(LLMFactory, "get_model_config", classmethod(lambda cls: fake_mc))
    monkeypatch.setattr(
        complete_mod,
        "resolve_model_billing_and_provider",
        lambda m: ("oauth", catalog_entries[m]["provider"]),
    )
    monkeypatch.setattr(
        uf,
        "_resolve_model_subscription_info",
        lambda m, **_kw: (
            catalog_entries[m]["provider"],
            catalog_entries[m]["provider"],
            catalog_entries[m]["model_id"],
            False,
        ),
    )

    now = datetime.now(timezone.utc)
    conn_row = {
        "account_id": "acct-r5",
        "email": "r5@everflow.test",
        "plan_type": "pro",
        "expires_at": now + timedelta(hours=2),
        "updated_at": now,
        "needs_reauth_at": None,
        "last_error_code": None,
        "is_live": True,
    }
    monkeypatch.setattr(
        oauth_tokens_db,
        "get_oauth_connection",
        create_autospec(oauth_tokens_db.get_oauth_connection, instance=False, return_value=conn_row),
    )
    cred_id = uf.resolve_oauth_credential_id_from_row(conn_row)
    opus_key = uf.build_quota_cooldown_key(
        user_id=user_id,
        model_name="claude-opus-5-5-oauth",
        provider="claude-oauth",
        credential_id=cred_id,
    )
    gemini_key = uf.build_quota_cooldown_key(
        user_id=user_id,
        model_name="gemini-4-hydrogen-high-oauth",
        provider="antigravity-oauth",
        credential_id=cred_id,
    )
    assert opus_key and gemini_key

    redis_store: dict[str, Any] = {
        opus_key: {
            "expires_at": (now + timedelta(minutes=40)).timestamp(),
            "refusal": {
                "reason": "plan_limit",
                "until": (now + timedelta(minutes=40)).isoformat(),
            },
        },
        gemini_key: {
            "expires_at": (now + timedelta(minutes=5)).timestamp(),
            "refusal": {"reason": "rate_limited"},
        },
    }

    class _FakeRedis:
        async def get(self, key: str) -> Any:
            return redis_store.get(key)

    from src.utils.cache import redis_cache as redis_cache_mod

    monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: _FakeRedis())
    orig_require_policy = account_svc.require_policy
    monkeypatch.setattr(
        account_svc,
        "require_policy",
        create_autospec(orig_require_policy, instance=False, return_value=ALLOW_ALL),
    )
    monkeypatch.setattr(
        user_models,
        "get_model_preference",
        create_autospec(user_models.get_model_preference, instance=False, return_value={}),
    )

    # Primary check on Gemini (rate_limited) still does NOT trigger switching
    gemini_primary_avail = await uf.inspect_subscription_availability(
        user_id, "gemini-4-hydrogen-high-oauth", now=now
    )
    assert gemini_primary_avail.available is True

    # Candidate check on Gemini (include_any_cooldown=True) marks it unavailable
    gemini_cand_avail = await uf.inspect_subscription_availability(
        user_id, "gemini-4-hydrogen-high-oauth", include_any_cooldown=True, now=now
    )
    assert gemini_cand_avail.available is False
    assert gemini_cand_avail.kind == "rate_limit_cooldown"
    assert gemini_cand_avail.refusal_code == "rate_limited"

    # Full pre-dispatch selection: Opus exhausted, Gemini rate_limited, Codex available -> selects Codex
    async def _fake_resolve_llm(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(llm=SimpleNamespace(name=args[2], fallback=[]))

    monkeypatch.setattr(
        auto_fb_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, instance=False, side_effect=_fake_resolve_llm),
    )

    cfg, rec = await auto_fb_mod.resolve_automation_turn_llm_config(
        SimpleNamespace(),
        user_id,
        "claude-opus-5-5-oauth",
        payer_user_id=user_id,
    )
    assert cfg.llm.name == "gpt-6.2-sol-oauth"
    assert rec is not None
    assert rec["original_model"] == "claude-opus-5-5-oauth"
    assert rec["fallback_model"] == "gpt-6.2-sol-oauth"
    assert rec["selected_route"]["model_key"] == "gpt-6.2-sol-oauth"


@pytest.mark.asyncio
async def test_reasoning_effort_rewritten_variant_reverified_against_whitelist_policy_and_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When reasoning_effort rewrites a candidate to another variant, the final model must be
    re-verified against whitelist + policy_refuses and recorded accurately in selected_route."""
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    user_id = "user-r5-effort"
    catalog_entries = {
        "claude-opus-5-5-oauth": {"provider": "claude-oauth", "model_id": "claude-opus-5-5"},
        "gemini-4-hydrogen-high-oauth": {
            "provider": "antigravity-oauth",
            "model_id": "gemini-4-hydrogen-high",
        },
        "gemini-4-hydrogen-low-oauth": {
            "provider": "antigravity-oauth",
            "model_id": "gemini-4-hydrogen-low",
        },
        "gpt-6.2-sol-oauth": {"provider": "codex-oauth", "model_id": "gpt-6.2-sol"},
    }
    fake_mc = _make_fake_model_config(catalog_entries)
    from src.llms.llm import LLM as LLMFactory

    monkeypatch.setattr(LLMFactory, "get_model_config", classmethod(lambda cls: fake_mc))
    monkeypatch.setattr(
        complete_mod,
        "resolve_model_billing_and_provider",
        lambda m: ("oauth", catalog_entries[m]["provider"]),
    )
    monkeypatch.setattr(
        uf,
        "_resolve_model_subscription_info",
        lambda m, **_kw: (
            catalog_entries[m]["provider"],
            catalog_entries[m]["provider"],
            catalog_entries[m]["model_id"],
            False,
        ),
    )

    now = datetime.now(timezone.utc)

    async def _avail(uid: str, mk: str, **_kw: Any) -> uf.SubscriptionAvailability:
        if mk == "claude-opus-5-5-oauth":
            return uf.SubscriptionAvailability(
                available=False,
                kind="quota_cooldown",
                refusal_code="plan_limit",
                provider="claude-oauth",
                reset_at=now + timedelta(minutes=20),
                cooldown_key="quota:u:claude-oauth:model:opus",
                model_name=mk,
            )
        return uf.SubscriptionAvailability(
            available=True, provider=catalog_entries[mk]["provider"], model_name=mk
        )

    monkeypatch.setattr(
        uf,
        "inspect_subscription_availability",
        create_autospec(uf.inspect_subscription_availability, instance=False, side_effect=_avail),
    )
    monkeypatch.setattr(
        user_models,
        "get_model_preference",
        create_autospec(user_models.get_model_preference, instance=False, return_value={}),
    )

    async def _resolve_rewriting_high_to_low(*args: Any, **kwargs: Any) -> Any:
        requested = args[2]
        effort = kwargs.get("reasoning_effort")
        final_name = (
            "gemini-4-hydrogen-low-oauth"
            if requested == "gemini-4-hydrogen-high-oauth" and effort == "low"
            else requested
        )
        return SimpleNamespace(llm=SimpleNamespace(name=final_name, fallback=[]), llm_client=object())

    monkeypatch.setattr(
        auto_fb_mod,
        "resolve_llm_config",
        create_autospec(
            llm_config_mod.resolve_llm_config,
            instance=False,
            side_effect=_resolve_rewriting_high_to_low,
        ),
    )
    orig_require_policy = account_svc.require_policy
    monkeypatch.setattr(
        account_svc,
        "require_policy",
        create_autospec(orig_require_policy, instance=False, return_value=ALLOW_ALL),
    )

    # Case A: low variant is NOT in whitelist -> rejected after effort rewrite, skips to gpt-6.2-sol-oauth
    pool_without_low = [
        "claude-opus-5-5-oauth",
        "gemini-4-hydrogen-high-oauth",
        "gpt-6.2-sol-oauth",
    ]
    monkeypatch.setattr(
        dalton_settings_mod,
        "get_dalton_config",
        lambda: _make_dalton_config_with_pool(pool_without_low),
    )
    cfg_a, rec_a = await auto_fb_mod.resolve_automation_turn_llm_config(
        SimpleNamespace(),
        user_id,
        "claude-opus-5-5-oauth",
        reasoning_effort="low",
        payer_user_id=user_id,
    )
    assert cfg_a.llm.name == "gpt-6.2-sol-oauth"
    assert rec_a is not None
    assert rec_a["fallback_model"] == "gpt-6.2-sol-oauth"
    assert rec_a["selected_route"]["model_key"] == "gpt-6.2-sol-oauth"

    # Case B: low variant IS in whitelist & allowed by policy -> accepted, selected_route records actual low model
    pool_with_low = [
        "claude-opus-5-5-oauth",
        "gemini-4-hydrogen-high-oauth",
        "gemini-4-hydrogen-low-oauth",
        "gpt-6.2-sol-oauth",
    ]
    monkeypatch.setattr(
        dalton_settings_mod,
        "get_dalton_config",
        lambda: _make_dalton_config_with_pool(pool_with_low),
    )
    cfg_b, rec_b = await auto_fb_mod.resolve_automation_turn_llm_config(
        SimpleNamespace(),
        user_id,
        "claude-opus-5-5-oauth",
        reasoning_effort="low",
        payer_user_id=user_id,
    )
    assert cfg_b.llm.name == "gemini-4-hydrogen-low-oauth"
    assert rec_b is not None
    assert rec_b["fallback_model"] == "gemini-4-hydrogen-low-oauth"
    assert rec_b["selected_route"]["model_key"] == "gemini-4-hydrogen-low-oauth"

    # Case C: low variant IS in whitelist, but org policy_refuses denies low -> skips to gpt-6.2-sol-oauth
    policy_denies_low: Policy = merge_policy(
        PolicyLayer(),
        PolicyLayer(
            models=ModelsPolicy(
                allowed=[
                    "claude-opus-5-5-oauth",
                    "gemini-4-hydrogen-high-oauth",
                    "gpt-6.2-sol-oauth",
                ]
            ),
            own_model_access=OwnModelAccessPolicy(subscriptions=True),
        ),
    )
    monkeypatch.setattr(
        account_svc,
        "require_policy",
        create_autospec(orig_require_policy, instance=False, return_value=policy_denies_low),
    )
    cfg_c, rec_c = await auto_fb_mod.resolve_automation_turn_llm_config(
        SimpleNamespace(),
        user_id,
        "claude-opus-5-5-oauth",
        reasoning_effort="low",
        payer_user_id=user_id,
    )
    assert cfg_c.llm.name == "gpt-6.2-sol-oauth"
    assert rec_c is not None
    assert rec_c["fallback_model"] == "gpt-6.2-sol-oauth"
    assert rec_c["selected_route"]["model_key"] == "gpt-6.2-sol-oauth"


@pytest.mark.asyncio
async def test_assist_json_parse_repair_preserves_fallback_model_and_ignore_custom_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When assist_json falls back to a whitelisted subscription model and its JSON needs repair,
    the repair request keeps the selected fallback model and ignore_custom_models=True."""
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    pool = ["gemini-4-hydrogen-high-oauth", "gpt-6.2-sol-oauth"]
    monkeypatch.setattr(
        dalton_settings_mod, "get_dalton_config", lambda: _make_dalton_config_with_pool(pool)
    )
    catalog_entries = {
        "gemini-4-hydrogen-high-oauth": {
            "provider": "antigravity-oauth",
            "model_id": "gemini-4-hydrogen-high",
        },
        "gpt-6.2-sol-oauth": {"provider": "codex-oauth", "model_id": "gpt-6.2-sol"},
    }
    fake_mc = _make_fake_model_config(catalog_entries)
    from src.llms.llm import LLM as LLMFactory

    monkeypatch.setattr(LLMFactory, "get_model_config", classmethod(lambda cls: fake_mc))
    monkeypatch.setattr(
        complete_mod,
        "resolve_model_billing_and_provider",
        lambda m: ("oauth", catalog_entries[m]["provider"]),
    )
    monkeypatch.setattr(
        uf,
        "inspect_subscription_availability",
        create_autospec(
            uf.inspect_subscription_availability,
            instance=False,
            side_effect=lambda uid, mk, **_kw: uf.SubscriptionAvailability(available=True, model_name=mk),
        ),
    )
    monkeypatch.setattr(
        assist_mod,
        "resolve_assist_model",
        create_autospec(
            assist_mod.resolve_assist_model,
            instance=False,
            side_effect=lambda role="assist", explicit_model=None, **_kw: explicit_model
            or "gemini-4-hydrogen-high-oauth",
        ),
    )

    calls: list[dict[str, Any]] = []

    async def _complete_with_repair(**kwargs: Any) -> complete_mod.DaltonCompleteResult:
        calls.append(kwargs)
        if kwargs["request_model"] == "gemini-4-hydrogen-high-oauth":
            return complete_mod.DaltonCompleteResult(
                ok=False,
                status="failed",
                model=kwargs["request_model"],
                error="plan limit reached",
                refusal_reason="plan_limit",
                quota_or_auth_trigger=True,
            )
        if not str(kwargs.get("scope_ref") or "").endswith(":parse_repair"):
            return complete_mod.DaltonCompleteResult(
                ok=True,
                status="done",
                model="gpt-6.2-sol-oauth",
                text="not valid json at all",
            )
        return complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            model="gpt-6.2-sol-oauth",
            data={"verdict": "supported"},
            text='{"verdict": "supported"}',
        )

    monkeypatch.setattr(
        assist_mod,
        "dalton_complete",
        create_autospec(complete_mod.dalton_complete, instance=False, side_effect=_complete_with_repair),
    )

    schema = {
        "type": "object",
        "properties": {"verdict": {"type": "string"}},
        "required": ["verdict"],
    }
    res = await assist_mod.assist_json(
        task="audit.judge",
        instructions="judge",
        payload={"x": 1},
        schema=schema,
        user_id="u-repair-1",
        model="gemini-4-hydrogen-high-oauth",
        fallback_models=["gpt-6.2-sol-oauth"],
    )
    assert res.ok is True
    assert res.model == "gpt-6.2-sol-oauth"
    assert len(calls) == 3
    assert calls[1]["request_model"] == "gpt-6.2-sol-oauth"
    assert calls[1]["ignore_custom_models"] is True
    assert calls[2]["request_model"] == "gpt-6.2-sol-oauth"
    assert calls[2]["ignore_custom_models"] is True


@pytest.mark.asyncio
async def test_transient_oauth_refresh_failure_on_401_does_not_trigger_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a 401 triggers OAuth refresh and refresh hits a transient failure (503 / timeout /
    OAuthRefreshUnavailable), the re-raised 401 must NOT be classified as a switch trigger."""
    from src.server.services.llm import clients as llm_clients

    class _Fake401(Exception):
        def __init__(self) -> None:
            super().__init__("Error code: 401 - invalid_api_key")
            self.status_code = 401

    def _tagged_client() -> Any:
        return oauth_refresh_mod.tag_oauth_client(
            _FakeModel("gpt-6.2-sol-oauth"),
            oauth_refresh_mod.OAuthRebuildSpec(
                user_id="u-1",
                model_name="gpt-6.2-sol-oauth",
                provider="codex-oauth",
                token_digest=oauth_refresh_mod.token_digest("t0"),
            ),
        )

    async def _fn_401(_client: Any) -> Any:
        raise _Fake401()

    orig_resolve_oauth = llm_clients.resolve_oauth_llm_client

    # 1) Transient refresh failures (HTTP 503 oauth_refresh_unavailable, TimeoutError, OAuthRefreshUnavailable)
    for transient_err in (
        HTTPException(status_code=503, detail={"type": "oauth_refresh_unavailable"}),
        TimeoutError("oauth refresh lock wait timeout"),
        OAuthRefreshUnavailable("codex-oauth", "refresh_failed"),
    ):
        monkeypatch.setattr(
            llm_clients,
            "resolve_oauth_llm_client",
            create_autospec(
                orig_resolve_oauth,
                instance=False,
                side_effect=transient_err,
            ),
        )
        with pytest.raises(_Fake401) as exc_info:
            await oauth_refresh_mod.call_with_oauth_refresh(_tagged_client(), _fn_401)
        assert oauth_refresh_mod.get_transient_oauth_refresh_error(exc_info.value) is transient_err
        assert uf.is_quota_or_auth_switch_trigger(exc_info.value) is False
        assert complete_mod._classify_exception(exc_info.value, provider="codex-oauth") == (
            "failed",
            "oauth_refresh_unavailable",
        )

    # 2) Confirmed terminal refresh failure (OAuthReauthRequired) STILL triggers fallback
    monkeypatch.setattr(
        llm_clients,
        "resolve_oauth_llm_client",
        create_autospec(
            orig_resolve_oauth,
            instance=False,
            side_effect=OAuthReauthRequired("codex-oauth", "invalid_grant"),
        ),
    )
    with pytest.raises(_Fake401) as term_exc:
        await oauth_refresh_mod.call_with_oauth_refresh(_tagged_client(), _fn_401)
    assert oauth_refresh_mod.get_transient_oauth_refresh_error(term_exc.value) is None
    assert uf.is_quota_or_auth_switch_trigger(term_exc.value) is True


@pytest.mark.asyncio
async def test_internal_llm_complete_checks_original_journal_before_switching_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate /internal/llm/complete requests must inspect the original model's journal
    state before pre-dispatch model switching so done/in_flight calls are not duplicated."""
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    pool = ["gemini-4-hydrogen-high-oauth", "gpt-6.2-sol-oauth"]
    monkeypatch.setattr(
        dalton_settings_mod, "get_dalton_config", lambda: _make_dalton_config_with_pool(pool)
    )
    monkeypatch.setattr(
        internal_llm_router, "get_allowed_host_oauth_models", lambda: frozenset(pool)
    )
    catalog_entries = {
        "gemini-4-hydrogen-high-oauth": {
            "provider": "antigravity-oauth",
            "model_id": "gemini-4-hydrogen-high",
        },
        "gpt-6.2-sol-oauth": {"provider": "codex-oauth", "model_id": "gpt-6.2-sol"},
    }
    fake_mc = _make_fake_model_config(catalog_entries)
    from src.llms.llm import LLM as LLMFactory

    monkeypatch.setattr(LLMFactory, "get_model_config", classmethod(lambda cls: fake_mc))
    monkeypatch.setattr(
        complete_mod,
        "resolve_model_billing_and_provider",
        lambda m: ("oauth", catalog_entries[m]["provider"]),
    )
    monkeypatch.setattr(
        uf,
        "_resolve_model_subscription_info",
        lambda m, **_kw: (
            catalog_entries[m]["provider"],
            catalog_entries[m]["provider"],
            catalog_entries[m]["model_id"],
            False,
        ),
    )
    from src.dalton import access_tiers

    monkeypatch.setattr(
        access_tiers,
        "get_primary_admin_user_id",
        create_autospec(access_tiers.get_primary_admin_user_id, instance=False, return_value="admin-1"),
    )

    now = datetime.now(timezone.utc)

    async def _avail_primary_cooled(uid: str, mk: str, **_kw: Any) -> uf.SubscriptionAvailability:
        if mk == "gemini-4-hydrogen-high-oauth":
            return uf.SubscriptionAvailability(
                available=False,
                kind="quota_cooldown",
                refusal_code="plan_limit",
                provider="antigravity-oauth",
                reset_at=now + timedelta(minutes=20),
                cooldown_key="quota:admin-1:antigravity-oauth:vendor:google",
                model_name=mk,
            )
        return uf.SubscriptionAvailability(available=True, provider="codex-oauth", model_name=mk)

    monkeypatch.setattr(
        uf,
        "inspect_subscription_availability",
        create_autospec(
            uf.inspect_subscription_availability,
            instance=False,
            side_effect=_avail_primary_cooled,
        ),
    )

    req = internal_llm_router.InternalLLMCompleteRequest(
        model="gemini-4-hydrogen-high-oauth",
        user_prompt="repeat-me",
        purpose="host_dedup_test",
    )

    # Case A: Original request already DONE -> must replay requested_model, never switch
    monkeypatch.setattr(
        internal_llm_router,
        "inspect_existing_completion_journal",
        create_autospec(
            complete_mod.inspect_existing_completion_journal,
            instance=False,
            return_value=("done", {"state": "done"}),
        ),
    )
    done_calls: list[dict[str, Any]] = []

    async def _complete_done(**kwargs: Any) -> complete_mod.DaltonCompleteResult:
        done_calls.append(kwargs)
        return complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            model=kwargs["model"],
            text="cached-answer",
            replayed=True,
            cached=True,
        )

    monkeypatch.setattr(
        internal_llm_router,
        "dalton_complete",
        create_autospec(complete_mod.dalton_complete, instance=False, side_effect=_complete_done),
    )
    resp_done = await internal_llm_router.internal_llm_complete(req)
    assert resp_done.ok is True
    assert resp_done.model == "gemini-4-hydrogen-high-oauth"
    assert [c["model"] for c in done_calls] == ["gemini-4-hydrogen-high-oauth"]

    # Case B: Original request IN_FLIGHT -> must keep requested_model and return in_flight
    monkeypatch.setattr(
        internal_llm_router,
        "inspect_existing_completion_journal",
        create_autospec(
            complete_mod.inspect_existing_completion_journal,
            instance=False,
            return_value=("in_flight", {"state": "pending"}),
        ),
    )
    inflight_calls: list[dict[str, Any]] = []

    async def _complete_inflight(**kwargs: Any) -> complete_mod.DaltonCompleteResult:
        inflight_calls.append(kwargs)
        return complete_mod.DaltonCompleteResult(
            ok=False,
            status="in_flight",
            model=kwargs["model"],
            error_kind="in_flight",
            error="in flight",
        )

    monkeypatch.setattr(
        internal_llm_router,
        "dalton_complete",
        create_autospec(complete_mod.dalton_complete, instance=False, side_effect=_complete_inflight),
    )
    resp_inflight = await internal_llm_router.internal_llm_complete(req)
    assert resp_inflight.ok is False
    assert resp_inflight.status == "in_flight"
    assert [c["model"] for c in inflight_calls] == ["gemini-4-hydrogen-high-oauth"]

    # Case C: Original request has no active record ("proceed") -> switches to Codex
    monkeypatch.setattr(
        internal_llm_router,
        "inspect_existing_completion_journal",
        create_autospec(
            complete_mod.inspect_existing_completion_journal,
            instance=False,
            return_value=("proceed", None),
        ),
    )
    proceed_calls: list[dict[str, Any]] = []

    async def _complete_proceed(**kwargs: Any) -> complete_mod.DaltonCompleteResult:
        proceed_calls.append(kwargs)
        return complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            model=kwargs["model"],
            text="fresh-from-codex",
        )

    monkeypatch.setattr(
        internal_llm_router,
        "dalton_complete",
        create_autospec(complete_mod.dalton_complete, instance=False, side_effect=_complete_proceed),
    )
    resp_proceed = await internal_llm_router.internal_llm_complete(req)
    assert resp_proceed.ok is True
    assert resp_proceed.model == "gpt-6.2-sol-oauth"
    assert [c["model"] for c in proceed_calls] == ["gpt-6.2-sol-oauth"]


@pytest.mark.asyncio
async def test_wakeup_non_subscription_held_model_error_keeps_account_default_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When switch is ON, an ordinary wakeup whose thread holds a custom model missing a BYOK
    key must still fall back to the account default model instead of failing."""
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    user_id = "user-r5-wakeup-byok"
    thread_id = str(uuid4())

    prefs = {
        "preferred_model": "gpt-6.2-sol-oauth",
        "custom_models": [
            {"name": "my-custom-byok", "provider": "openai", "model_id": "gpt-custom"}
        ],
    }
    monkeypatch.setattr(
        user_models,
        "get_model_preference",
        create_autospec(user_models.get_model_preference, instance=False, return_value=prefs),
    )
    monkeypatch.setattr(
        auto_tm_mod,
        "_resolve_automation_notification_context",
        create_autospec(
            auto_tm_mod._resolve_automation_notification_context,
            instance=False,
            return_value=(None, None, prefs, False, False, None),
        ),
    )
    monkeypatch.setattr(
        uf,
        "inspect_subscription_availability",
        create_autospec(
            uf.inspect_subscription_availability,
            instance=False,
            side_effect=lambda uid, mk, **_kw: uf.SubscriptionAvailability(available=True, model_name=mk),
        ),
    )

    from src.server.services.llm import thread_model as tm_mod

    monkeypatch.setattr(
        tm_mod,
        "turn_model",
        create_autospec(tm_mod.turn_model, instance=False, return_value="my-custom-byok"),
    )

    resolved_calls: list[str | None] = []

    async def _resolve_impl(model_name: str | None) -> Any:
        resolved_calls.append(model_name)
        if model_name == "my-custom-byok":
            raise HTTPException(
                status_code=400,
                detail={"type": "byok_key_required", "message": "Missing BYOK API key"},
            )
        return SimpleNamespace(llm=SimpleNamespace(name="gpt-6.2-sol-oauth"))

    resolve_mock = create_autospec(_resolve_impl, side_effect=_resolve_impl)
    fallback_records: list[dict[str, Any]] = []
    wakeup_req = SimpleNamespace(
        query_type="wakeup",
        platform=None,
        hitl_response=None,
        wakeup={"v": 1, "notices": [{"wakeup_id": "w-byok"}]},
    )

    cfg = await auto_tm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=user_id,
        thread_id=thread_id,
        request=wakeup_req,
        requested_model=None,
        held_model="my-custom-byok",
        from_service=True,
        payer_user_id=user_id,
        resolve=resolve_mock,
        on_fallback=fallback_records.append,
    )
    assert cfg.llm.name == "gpt-6.2-sol-oauth"
    assert resolved_calls == ["my-custom-byok", None]
    assert fallback_records == []
