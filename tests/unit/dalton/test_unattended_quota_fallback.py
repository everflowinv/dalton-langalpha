"""Unit tests for Prod Audit 2 §2: unattended-turn subscription quota fallback.

Covers every production path and review item with real call chains and signature-checked doubles:
- Default-off switches & d062 migration (including ``seen_step_keys``).
- Smoke test importing and invoking every public entrypoint of ``unattended_fallback`` with switch ON
  (including ``resolve_unattended_fallback_clients`` calling real ``src.llms.preferences.resolve_turn_tuning``).
- Trusted server-side unattended source classification (client spoofing rejected).
- Real OAuth auth probe via ``get_oauth_connection`` + ``connection_state`` and Redis cooldown key parity
  with ``model_resilience.quota_redis_key`` (including ``credential_id``).
- Profile-aware candidate chain: ``disable_fallback: true`` only disables personal list; cash-only
  personal list filtered empty continues into ``resolve_profile()`` default ``$0`` subscription chain.
- Pre-dispatch cooldown -> subscription pool fallback via real ``resolve_turn_config_with_automation_inheritance``
  and ``resolve_automation_turn_llm_config``.
- Unified cash admission via ``acquire_cash_lease_tx`` across pre-dispatch, legacy auth-failure
  (``resolve_with_subscription_fallback``), and runtime ``ModelResilienceMiddleware``; cash never chosen
  when cash switch is OFF, and bound onto ``CreditLease`` when ON.
- Real ``Policy`` / ``PolicyLayer`` re-verification via ``policy_refuses``, START snapshot inclusion of
  ``unattended_fallback_models``, runtime persistence of ``active_fallback_route``, and resume verification.
- Error classification contrast: switches OFF -> terminal 429 stays recoverable; switch ON + interactive ->
  recoverable; switch ON + trusted unattended -> non-recoverable.
- ``/internal/llm/complete`` forcing ``ignore_custom_models=True`` and structured trigger only (plain 429 /
  text containing ``billing`` never re-sends).
- ``assist_json`` structured quota/auth trigger only when enabled.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from langchain_core.messages import AIMessage

from src.config.models import ModelsPolicy, OwnModelAccessPolicy, PolicyLayer
from src.dalton.budget import port as budget_port
from src.dalton.llm import assist as assist_mod
from src.dalton.llm import complete as complete_mod
from src.dalton.llm import oauth_refresh as oauth_refresh_mod
from src.dalton.llm import unattended_fallback as uf
from src.dalton.services import automation_subscription_fallback as auto_fb_mod
from src.dalton.services import automation_thread_model as auto_tm_mod
from src.dalton.services.team_model_catalog.constants import TURN_SNAPSHOT_SCHEMA
from src.dalton.services.team_model_catalog.snapshot import (
    RouteFingerprint,
    bind_catalog_snapshot,
    verify_candidate_route_against_snapshot,
    verify_resume_route,
)
from src.dalton.settings import (
    allow_cash_fallback_on_unattended,
    is_unattended_quota_fallback_enabled,
)
from src.llms.provider_errors import ProviderRefusal
from ptc_agent.agent.middleware.credit_gate import (
    CreditGateState,
    CreditLease,
    LeaseVerdict,
    current_credit_gate,
)
from src.ptc_agent.agent.middleware.model_resilience import (
    ModelResilienceMiddleware,
    _RemoteQuotaExhaustedError,
    quota_redis_key,
)
from src.server.database import oauth_tokens as oauth_tokens_db
from src.server.handlers.chat.error_handling import classify_error
from src.server.services.llm import clients as llm_clients
from src.server.services.llm import user_models
from src.server.services.policy import ALLOW_ALL, Policy, merge as merge_policy

REPO_ROOT = Path(__file__).resolve().parents[3]
D062_PATH = REPO_ROOT / "src" / "dalton" / "migrations" / "versions" / "d062_wakeup_quota_deferrals.py"


class _FakeModel:
    """Minimal chat-model test double with explicit call metadata."""

    def __init__(self, name: str, billing_type: str = "oauth", provider: str | None = None) -> None:
        self.model = name
        self._call_metadata = {
            "catalog_model": name,
            "manifest_model": name,
            "billing_type": billing_type,
            "pricing_provider": provider or ("anthropic-oauth" if billing_type == "oauth" else "deepseek"),
        }


class _FakeRequest:
    def __init__(
        self,
        model: Any,
        *,
        messages: list[Any] | None = None,
        model_settings: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.messages = messages or [SimpleNamespace(id="msg-1", content="hi")]
        self.model_settings = model_settings or {}
        self.state = {"messages": self.messages}
        self.system_message = None

    def override(self, **kwargs: Any) -> "_FakeRequest":
        nxt = _FakeRequest(
            kwargs.get("model", self.model),
            messages=kwargs.get("messages", self.messages),
            model_settings=kwargs.get("model_settings", self.model_settings),
        )
        nxt.system_message = kwargs.get("system_message", self.system_message)
        return nxt


class _FakeResponse:
    def __init__(self, text: str = "ok") -> None:
        self.result = [AIMessage(content=text)]
        self.response_metadata: dict[str, Any] = {}


class _FakeCursor:
    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state
        self._result: Any = None

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self._state.setdefault("queries", []).append((sql, params))
        sql_norm = " ".join(sql.split()).upper()
        if sql_norm.startswith("INSERT INTO DALTON_BUDGET_RESERVATIONS"):
            row = self._state.get("row")
            if row is None:
                row = {
                    "user_id": params[0],
                    "budget_day": params[1],
                    "run_ref": params[2],
                    "reserved_usd": Decimal("0"),
                    "observed_usd": params[5],
                    "usage_usd": Decimal("0"),
                    "task_cap_usd": params[6],
                    "state": "open",
                    "generation": 1,
                    "last_fallback_call_id": None,
                }
                self._state["row"] = row
            self._result = dict(row)
        elif sql_norm.startswith("UPDATE DALTON_BUDGET_RESERVATIONS"):
            row = self._state["row"]
            if "SET OBSERVED_USD" in sql_norm:
                row["observed_usd"] = max(Decimal(str(row["observed_usd"])), Decimal(str(params[0])))
                self._result = dict(row)
            elif "SET RESERVED_USD" in sql_norm:
                row["reserved_usd"] = Decimal(str(params[0]))
                row["generation"] = int(row.get("generation") or 1) + 1
                row["state"] = "open"
                if "LAST_FALLBACK_CALL_ID" in sql_norm:
                    row["last_fallback_call_id"] = params[2]
                self._result = {"generation": row["generation"]}
            elif "SET LAST_FALLBACK_CALL_ID" in sql_norm:
                row["last_fallback_call_id"] = params[0]
                self._result = None
        else:
            self._result = None

    async def fetchone(self) -> Any:
        return self._result


class _FakeConn:
    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state

    def cursor(self, **kwargs: Any) -> _FakeCursor:
        return _FakeCursor(self._state)


# ---------------------------------------------------------------------------
# 1. Settings switches & d062 migration
# ---------------------------------------------------------------------------


def test_unattended_switches_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", raising=False)
    monkeypatch.delenv("DALTON_ALLOW_CASH_FALLBACK_ON_UNATTENDED", raising=False)
    assert is_unattended_quota_fallback_enabled() is False
    assert allow_cash_fallback_on_unattended() is False

    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("DALTON_ALLOW_CASH_FALLBACK_ON_UNATTENDED", "true")
    assert is_unattended_quota_fallback_enabled() is True
    assert allow_cash_fallback_on_unattended() is True


def test_d062_migration_single_head_no_src_imports_and_sql() -> None:
    cfg = AlembicConfig(str(REPO_ROOT / "src" / "dalton" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "src" / "dalton" / "migrations"))
    script_dir = ScriptDirectory.from_config(cfg)
    assert script_dir.get_heads() == ["d062"]

    mod = importlib.import_module("src.dalton.migrations.versions.d062_wakeup_quota_deferrals")
    assert mod.revision == "d062"
    assert mod.down_revision == "d061"

    tree = ast.parse(D062_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not (alias.name == "src" or alias.name.startswith("src."))
        elif isinstance(node, ast.ImportFrom):
            m = node.module or ""
            assert not (m == "src" or m.startswith("src."))

    up_sql = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "src/dalton/alembic.ini", "upgrade", "d061:d062", "--sql"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert "dalton_wakeup_quota_deferrals" in up_sql.stdout
    assert "seen_step_keys" in up_sql.stdout
    assert "last_fallback_call_id" in up_sql.stdout

    down_sql = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "src/dalton/alembic.ini", "downgrade", "d062:d061", "--sql"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert "DROP TABLE IF EXISTS dalton_wakeup_quota_deferrals" in down_sql.stdout
    assert "DROP COLUMN IF EXISTS last_fallback_call_id" in down_sql.stdout


# ---------------------------------------------------------------------------
# 2. Live entrypoint smoke test with switch ON (catches missing imports like tuning!)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_entrypoints_import_and_execute_with_switch_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise every public entrypoint of unattended_fallback with switch ON (real resolve_turn_tuning)."""
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    monkeypatch.delenv("DALTON_ALLOW_CASH_FALLBACK_ON_UNATTENDED", raising=False)

    # 1. Context binding & source classification
    ctx = uf.UnattendedTurnContext(
        is_unattended=True,
        subject_user_id="user-smoke",
        payer_user_id="user-smoke",
        source_kind="automation",
        task_fallbacks=("gpt-6.2-sol-oauth",),
        run_id="run-smoke-1",
    )
    assert uf.is_unattended_turn(run_metadata={"automation_execution_id": "exec-1"}) is True
    assert uf.classify_unattended_source_kind(run_metadata={"automation_execution_id": "exec-1"}) == "automation"

    # 2. resolve_unattended_fallback_clients exercises REAL src.llms.preferences.resolve_turn_tuning
    fake_client = _FakeModel("gpt-6.2-sol-oauth", billing_type="oauth", provider="codex-oauth")
    resolve_client_mock = create_autospec(llm_clients.resolve_model_client, instance=False)
    resolve_client_mock.return_value = SimpleNamespace(client=fake_client)
    monkeypatch.setattr(llm_clients, "resolve_model_client", resolve_client_mock)
    monkeypatch.setattr(
        uf,
        "_classify_candidate_billing",
        lambda model_name, prefs=None, ignore_custom_models=False: ("oauth", "codex-oauth"),
    )
    monkeypatch.setattr(
        uf,
        "_resolve_model_subscription_info",
        lambda model_name, prefs=None, ignore_custom_models=False: ("codex-oauth", "codex-oauth", model_name, False),
    )

    cfg = SimpleNamespace(
        llm=SimpleNamespace(name="claude-opus-5-5-oauth", fallback=[]),
        fallback_llm_clients=[],
        fallback_llm_names=[],
        prompt_guidance=None,
        compaction=SimpleNamespace(profile=None),
    )
    with uf.bind_unattended_turn_context(ctx):
        assert uf.get_active_unattended_turn_context() == ctx
        handled = await uf.resolve_unattended_fallback_clients(
            cfg,
            "user-smoke",
            {"fallback_models": ["gpt-6.2-sol-oauth"]},
            {},
            is_byok=True,
            cache_key="k",
            policy=ALLOW_ALL,
            reasoning_effort="medium",
            fast_mode=False,
        )
    assert handled is True
    assert cfg.fallback_llm_names == ["gpt-6.2-sol-oauth"]
    assert len(cfg.fallback_llm_clients) == 1
    assert uf.is_quota_or_auth_only_client(cfg.fallback_llm_clients[0]) is True
    meta = uf.get_unattended_fallback_meta(cfg.fallback_llm_clients[0])
    assert meta is not None and meta["run_id"] == "run-smoke-1" and meta["payer_user_id"] == "user-smoke"

    # 3. Remaining public helpers (notice, request annotation, start metadata, failure payload, UUID key)
    assert "订阅额度" in uf.format_unattended_quota_notice("subscription_quota_exhausted", model_name="m1")
    req_body: dict[str, Any] = {"wakeup": {"v": 1, "notices": []}}
    uf.annotate_wakeup_request_body(
        req_body,
        job_id="11111111-1111-1111-1111-111111111111",
        attempts=2,
        payload={},
        created_at=datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc),
    )
    assert req_body["wakeup"]["attempts"] == 2
    start_meta = uf.build_wakeup_quota_start_metadata(
        SimpleNamespace(query_type="wakeup", wakeup=req_body["wakeup"], llm_model="m1"),
        user_id="user-smoke",
        workspace_id="ws-1",
        effective_model="m1",
    )
    assert "dalton_wakeup_quota_ctx" in start_meta
    fail_payload = uf.extract_mid_turn_quota_failure_payload(
        _RemoteQuotaExhaustedError("plan_limit", 429, provider_refusal=ProviderRefusal("plan_limit")),
        model_name="m1",
    )
    assert fail_payload is not None and fail_payload["refusal_code"] == "plan_limit"
    uid = uf.deterministic_wakeup_resume_request_key("11111111-1111-1111-1111-111111111111", 1)
    assert len(uid) == 36


# ---------------------------------------------------------------------------
# 3. Trusted server-side unattended source detection
# ---------------------------------------------------------------------------


def test_trusted_unattended_source_classification() -> None:
    assert uf.is_unattended_turn(run_metadata={"automation_execution_id": "exec-1"}) is True
    assert uf.is_unattended_turn(run_metadata={"automation_id": "auto-1"}) is True
    assert uf.is_unattended_turn(SimpleNamespace(origin=SimpleNamespace(type="automation"))) is True
    assert uf.is_unattended_turn(query_type="wakeup", platform=None, from_service=True) is True
    assert uf.is_unattended_turn(query_type="market_target", platform=None, from_service=True) is True
    assert uf.is_unattended_turn(query_type="system", platform=None, from_service=True) is True
    assert uf.is_unattended_turn(run_row={"metadata": {"unwatched": True}}) is True
    assert uf.is_unattended_turn(run_metadata={"report_back_ptc_thread_id": "t-1"}) is True
    assert uf.is_unattended_turn(internal_endpoint=True) is True
    assert uf.is_unattended_turn(assist_call=True) is True

    # Interactive user chat & client spoofing rejected
    assert uf.is_unattended_turn(query_type="initial", platform="web", from_service=False) is False
    assert uf.is_unattended_turn(query_type="wakeup", platform=None, from_service=False) is False
    assert uf.is_unattended_turn(query_type="wakeup", platform="feishu", from_service=True) is False
    assert uf.is_unattended_turn(query_type="follow_up", platform=None, from_service=True) is False


# ---------------------------------------------------------------------------
# 4. OAuth auth probe (real get_oauth_connection) & Redis key parity with credential_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_subscription_availability_oauth_reauth_and_redis_key_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
    reset_dt = now + timedelta(hours=2)
    updated_at = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        uf,
        "_resolve_model_subscription_info",
        lambda model_name, prefs=None, ignore_custom_models=False: (
            "codex-oauth",
            "codex-oauth",
            "gpt-6.2-sol-internal-id",
            False,
        ),
    )

    orig_get_oauth_connection = oauth_tokens_db.get_oauth_connection

    # Case A: Flagged OAuth row detected via real get_oauth_connection + connection_state
    flagged_conn_mock = create_autospec(orig_get_oauth_connection, instance=False)
    flagged_conn_mock.return_value = {
        "account_id": "acct-42",
        "email": "pm@example.com",
        "updated_at": updated_at,
        "needs_reauth_at": now - timedelta(minutes=5),
        "last_error_code": "invalid_grant",
    }
    monkeypatch.setattr(oauth_tokens_db, "get_oauth_connection", flagged_conn_mock)

    avail_auth = await uf.inspect_subscription_availability("u1", "gpt-6.2-sol-oauth", now=now)
    assert avail_auth.available is False
    assert avail_auth.kind == "auth_unavailable"
    assert avail_auth.refusal_code == "invalid_grant"

    # Case B: Healthy OAuth row produces the EXACT same Redis cooldown key (with credential_id!)
    # as ModelResilienceMiddleware's quota_redis_key on a tagged OAuth client.
    healthy_row = {
        "account_id": "acct-42",
        "email": "pm@example.com",
        "updated_at": updated_at,
        "needs_reauth_at": None,
        "last_error_code": None,
    }
    healthy_conn_mock = create_autospec(orig_get_oauth_connection, instance=False)
    healthy_conn_mock.return_value = healthy_row
    monkeypatch.setattr(oauth_tokens_db, "get_oauth_connection", healthy_conn_mock)

    tagged_client = _FakeModel("gpt-6.2-sol-oauth", billing_type="oauth", provider="codex-oauth")
    oauth_refresh_mod.tag_oauth_client(
        tagged_client,
        oauth_refresh_mod.OAuthRebuildSpec(
            user_id="u1",
            model_name="gpt-6.2-sol-oauth",
            provider="codex-oauth",
            account_id="acct-42",
            credential_id=oauth_refresh_mod.credential_identity(
                account_id="acct-42",
                email="pm@example.com",
                updated_at=updated_at,
            ),
        ),
    )
    runtime_key = quota_redis_key("gpt-6.2-sol-oauth", tagged_client, user_id="u1")
    assert runtime_key is not None

    stored_by_key = {
        runtime_key: {
            "expires_at": reset_dt.timestamp(),
            "refusal": {"reason": "plan_limit", "until": reset_dt.isoformat()},
        }
    }

    class _FakeCache:
        async def get(self, key: str) -> Any:
            return stored_by_key.get(key)

    from src.utils.cache import redis_cache

    monkeypatch.setattr(redis_cache, "get_cache_client", lambda: _FakeCache())
    avail_cool = await uf.inspect_subscription_availability("u1", "gpt-6.2-sol-oauth", now=now)
    assert avail_cool.cooldown_key == runtime_key
    assert avail_cool.available is False
    assert avail_cool.kind == "quota_cooldown"
    assert avail_cool.reset_at == reset_dt


# ---------------------------------------------------------------------------
# 5. Candidate chain: disable_fallback & filtered-empty personal list keep profile $0 pool
# ---------------------------------------------------------------------------


def test_resolve_unattended_fallback_chain_disable_and_cash_filter_fall_through_to_profile_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DALTON_ALLOW_CASH_FALLBACK_ON_UNATTENDED", raising=False)
    billing_map = {
        "claude-opus-5-5-oauth": ("oauth", "anthropic-oauth"),
        "gpt-6.2-sol-oauth": ("oauth", "codex-oauth"),
        "gemini-4-neon-high-oauth": ("oauth", "antigravity-oauth"),
        "deepseek-flash": ("cash", "deepseek"),
    }
    monkeypatch.setattr(
        uf,
        "_classify_candidate_billing",
        lambda model_name, prefs=None, ignore_custom_models=False: billing_map.get(
            model_name, ("cash", "unknown")
        ),
    )
    monkeypatch.setattr(
        uf,
        "get_profile_default_fallback_chain",
        lambda: ["gpt-6.2-sol-oauth", "deepseek-flash", "gemini-4-neon-high-oauth"],
    )

    # disable_fallback: true disables ONLY personal fallback_models, STILL using profile default $0 chain
    chain_disabled_personal = uf.resolve_unattended_fallback_chain(
        user_prefs={
            "disable_fallback": True,
            "fallback_models": ["gemini-4-neon-high-oauth"],
        },
        primary_model="claude-opus-5-5-oauth",
    )
    assert chain_disabled_personal == ["gpt-6.2-sol-oauth", "gemini-4-neon-high-oauth"]

    # Non-empty personal list containing only cash models gets filtered empty -> continues with profile $0 pool
    chain_cash_only_personal = uf.resolve_unattended_fallback_chain(
        user_prefs={"fallback_models": ["deepseek-flash"]},
        primary_model="claude-opus-5-5-oauth",
    )
    assert chain_cash_only_personal == ["gpt-6.2-sol-oauth", "gemini-4-neon-high-oauth"]

    # When cash switch is ON, cash model is included ahead of profile tail
    chain_cash_on = uf.resolve_unattended_fallback_chain(
        user_prefs={"fallback_models": ["deepseek-flash"]},
        primary_model="claude-opus-5-5-oauth",
        allow_cash=True,
    )
    assert chain_cash_on == ["deepseek-flash", "gpt-6.2-sol-oauth", "gemini-4-neon-high-oauth"]


# ---------------------------------------------------------------------------
# 6. Pre-dispatch cooldown -> subscription pool fallback (real chain)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_dispatch_cooldown_switches_to_subscription_pool_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real chain: resolve_turn_config_with_automation_inheritance probes cooldown -> picks $0 fallback."""
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    monkeypatch.delenv("DALTON_ALLOW_CASH_FALLBACK_ON_UNATTENDED", raising=False)
    now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)

    pref_mock = create_autospec(user_models.get_model_preference, instance=False)
    pref_mock.return_value = {"default_model": "claude-opus-5-5-oauth", "fallback_models": ["deepseek-flash"]}
    monkeypatch.setattr(user_models, "get_model_preference", pref_mock)

    monkeypatch.setattr(
        uf,
        "get_profile_default_fallback_chain",
        lambda: ["gpt-6.2-sol-oauth"],
    )
    monkeypatch.setattr(
        uf,
        "_classify_candidate_billing",
        lambda model_name, prefs=None, ignore_custom_models=False: (
            ("oauth", "codex-oauth") if model_name.endswith("-oauth") else ("cash", "deepseek")
        ),
    )

    avail_map = {
        "claude-opus-5-5-oauth": uf.SubscriptionAvailability(
            available=False,
            kind="quota_cooldown",
            refusal_code="plan_limit",
            provider="anthropic-oauth",
            reset_at=now + timedelta(hours=1),
            cooldown_key="ck:anthropic",
            model_name="claude-opus-5-5-oauth",
        ),
        "gpt-6.2-sol-oauth": uf.SubscriptionAvailability(
            available=True,
            provider="codex-oauth",
            cooldown_key="ck:codex",
            model_name="gpt-6.2-sol-oauth",
        ),
    }

    async def _fake_avail(
        user_id: str,
        model_name: str,
        *,
        client: Any = None,
        prefs: Any = None,
        ignore_custom_models: bool = False,
        now: Any = None,
    ) -> uf.SubscriptionAvailability:
        return avail_map[model_name]

    monkeypatch.setattr(uf, "inspect_subscription_availability", _fake_avail)
    monkeypatch.setattr(
        uf,
        "resolve_live_route_fingerprint",
        create_autospec(
            uf.resolve_live_route_fingerprint,
            instance=False,
            return_value=RouteFingerprint(
                model_key="gpt-6.2-sol-oauth",
                provider="codex-oauth",
                model_id="gpt-6.2-sol",
                billing="subscription",
            ),
        ),
    )
    from src.server.services import account as account_mod

    monkeypatch.setattr(
        account_mod,
        "require_policy",
        create_autospec(account_mod.require_policy, instance=False, return_value=ALLOW_ALL),
    )
    monkeypatch.setattr(
        auto_tm_mod,
        "_resolve_automation_notification_context",
        create_autospec(
            auto_tm_mod._resolve_automation_notification_context,
            instance=False,
            return_value=(None, None, None, False, False, None),
        ),
    )

    resolved_models: list[str | None] = []

    async def _resolve(model_name: str | None) -> Any:
        resolved_models.append(model_name)
        return SimpleNamespace(llm=SimpleNamespace(name=model_name))

    fallback_events: list[dict[str, Any]] = []
    req = SimpleNamespace(query_type="wakeup", platform=None, hitl_response=None)
    cfg = await auto_tm_mod.resolve_turn_config_with_automation_inheritance(
        user_id="user-1",
        thread_id="thread-1",
        request=req,
        requested_model="claude-opus-5-5-oauth",
        held_model=None,
        from_service=True,
        run_id="run-pre-1",
        payer_user_id="user-1",
        resolve=_resolve,
        on_fallback=fallback_events.append,
    )
    assert cfg.llm.name == "gpt-6.2-sol-oauth"
    assert resolved_models == ["gpt-6.2-sol-oauth"]
    assert len(fallback_events) == 1
    assert fallback_events[0]["fallback_model"] == "gpt-6.2-sol-oauth"
    assert getattr(cfg, "_dalton_active_fallback_route", {}).get("model_key") == "gpt-6.2-sol-oauth"


# ---------------------------------------------------------------------------
# 7. Unified cash admission across legacy auth-failure, pre-dispatch & runtime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_auth_failure_blocks_cash_when_off_and_uses_acquire_cash_lease_when_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve_with_subscription_fallback with unattended_gate=True never picks cash when OFF,
    and routes through acquire_cash_lease_tx (binding LeaseVerdict) when ON."""
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    monkeypatch.setattr(uf, "get_profile_default_fallback_chain", lambda: [])
    monkeypatch.setattr(
        uf,
        "_classify_candidate_billing",
        lambda model_name, prefs=None, ignore_custom_models=False: ("cash", "deepseek"),
    )
    monkeypatch.setattr(
        uf,
        "resolve_live_route_fingerprint",
        create_autospec(
            uf.resolve_live_route_fingerprint,
            instance=False,
            return_value=RouteFingerprint(
                model_key="deepseek-flash",
                provider="deepseek",
                model_id="deepseek-chat",
                billing="cash",
            ),
        ),
    )
    from src.server.services import account as account_mod

    monkeypatch.setattr(
        account_mod,
        "require_policy",
        create_autospec(account_mod.require_policy, instance=False, return_value=ALLOW_ALL),
    )
    monkeypatch.setattr(
        auto_fb_mod,
        "_subscription_provider_for_model",
        create_autospec(auto_fb_mod._subscription_provider_for_model, instance=False, return_value=None),
    )

    orig_exc = HTTPException(status_code=400, detail={"type": "oauth_reauth_required", "provider": "anthropic-oauth"})

    async def _resolve_cand(model_name: str) -> Any:
        return SimpleNamespace(llm=SimpleNamespace(name=model_name), llm_client=_FakeModel(model_name, "cash"))

    # Cash switch OFF -> raises original exception, never picks deepseek-flash
    monkeypatch.delenv("DALTON_ALLOW_CASH_FALLBACK_ON_UNATTENDED", raising=False)
    with pytest.raises(HTTPException) as exc_info:
        await auto_fb_mod.resolve_with_subscription_fallback(
            user_id="user-1",
            prefs={"fallback_models": ["deepseek-flash"]},
            original_model="claude-opus-5-5-oauth",
            failed_provider="anthropic-oauth",
            reason="oauth_reauth_required",
            resolve_candidate=_resolve_cand,
            exc_to_raise=orig_exc,
            model_resolves_fn=lambda prefs, name: True,
            unattended_gate=True,
            payer_user_id="user-1",
            run_id="run-cash-auth-1",
        )
    assert exc_info.value is orig_exc

    # Cash switch ON -> MUST call acquire_cash_lease with real payer & run_id & bind verdict
    monkeypatch.setenv("DALTON_ALLOW_CASH_FALLBACK_ON_UNATTENDED", "1")
    lease_mock = create_autospec(uf.acquire_cash_lease, instance=False)
    lease_mock.return_value = LeaseVerdict(granted=True, ceiling_credits=250.0, ttl_seconds=300.0, generation=2)
    monkeypatch.setattr(uf, "acquire_cash_lease", lease_mock)

    cfg, rec = await auto_fb_mod.resolve_with_subscription_fallback(
        user_id="user-1",
        prefs={"fallback_models": ["deepseek-flash"]},
        original_model="claude-opus-5-5-oauth",
        failed_provider="anthropic-oauth",
        reason="oauth_reauth_required",
        resolve_candidate=_resolve_cand,
        exc_to_raise=orig_exc,
        model_resolves_fn=lambda prefs, name: True,
        unattended_gate=True,
        payer_user_id="user-1",
        run_id="run-cash-auth-1",
    )
    assert cfg.llm.name == "deepseek-flash"
    assert rec["fallback_model"] == "deepseek-flash"
    assert lease_mock.await_count == 1
    assert lease_mock.await_args.kwargs["user_id"] == "user-1"
    assert lease_mock.await_args.kwargs["run_ref"] == "run-cash-auth-1"
    assert getattr(cfg, "_dalton_pre_cash_lease_verdict").ceiling_credits == 250.0


# ---------------------------------------------------------------------------
# 8. ModelResilienceMiddleware: transient 502 stays on primary; quota 429 switches,
#    binds CreditLease, and persists active_fallback_route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resilience_transient_502_stays_on_primary_quota_429_switches_and_persists_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _FakeModel("claude-opus-5-5-oauth", billing_type="oauth")
    fallback = _FakeModel("deepseek-flash", billing_type="cash", provider="deepseek")
    uf.stamp_quota_or_auth_only(
        fallback,
        candidate_name="deepseek-flash",
        subject_user_id="user-1",
        payer_user_id="user-1",
        run_id="run-resil-1",
        allow_cash=True,
    )

    route_fp = RouteFingerprint(
        model_key="deepseek-flash",
        provider="deepseek",
        model_id="deepseek-chat",
        bound_base_url="https://api.deepseek.com",
        key_fingerprint=None,
        billing="cash",
    )
    snap = {
        "schema": TURN_SNAPSHOT_SCHEMA,
        "models": {
            "deepseek-flash": {
                "model_key": "deepseek-flash",
                "provider": "deepseek",
                "model_id": "deepseek-chat",
                "billing": "cash",
                "bound_base_url": "https://api.deepseek.com",
                "key_fingerprint": None,
            }
        },
        "variants": {},
    }

    monkeypatch.setattr(
        uf,
        "resolve_live_route_fingerprint",
        create_autospec(uf.resolve_live_route_fingerprint, instance=False, return_value=route_fp),
    )
    from src.server.services import account as account_mod

    monkeypatch.setattr(
        account_mod,
        "require_policy",
        create_autospec(account_mod.require_policy, instance=False, return_value=ALLOW_ALL),
    )
    lease_mock = create_autospec(uf.acquire_cash_lease, instance=False)
    lease_mock.return_value = LeaseVerdict(granted=True, ceiling_credits=500.0, ttl_seconds=300.0, generation=3)
    monkeypatch.setattr(uf, "acquire_cash_lease", lease_mock)
    persist_mock = create_autospec(uf.persist_active_fallback_route, instance=False, return_value=True)
    monkeypatch.setattr(uf, "persist_active_fallback_route", persist_mock)

    mw = ModelResilienceMiddleware(
        primary_name="claude-opus-5-5-oauth",
        primary_client=primary,
        fallbacks=[("deepseek-flash", fallback)],
        max_retries=1,
        initial_delay=0.0,
        jitter=False,
    )

    # Case A: Transient 502 must NOT switch to quota_or_auth_only fallback
    seen_models_502: list[str] = []

    async def _handler_502(req: Any) -> Any:
        seen_models_502.append(req.model.model)
        err = RuntimeError("Bad Gateway 502")
        err.status_code = 502  # type: ignore[attr-defined]
        raise err

    with pytest.raises(RuntimeError, match="502"):
        await mw.awrap_model_call(_FakeRequest(primary), _handler_502)
    assert all(m == "claude-opus-5-5-oauth" for m in seen_models_502)
    assert lease_mock.await_count == 0
    assert persist_mock.await_count == 0

    # Case B: Terminal quota 429 switches through real acquire_fallback_candidate_live ->
    # binds CreditLease verdict AND persists active_fallback_route!
    seen_models_429: list[str] = []

    async def _handler_429(req: Any) -> Any:
        seen_models_429.append(req.model.model)
        if req.model.model == "claude-opus-5-5-oauth":
            raise _RemoteQuotaExhaustedError(
                "Plan limit reached",
                429,
                provider_refusal=ProviderRefusal("plan_limit"),
            )
        return _FakeResponse("fallback-answer")

    lease = CreditLease(
        port=SimpleNamespace(),
        recorded_as="user-1",
        run_ref="run-resil-1",
        is_byok=False,
        sender="user-1",
    )
    gate_state = CreditGateState(run_ref="run-resil-1", kind="run", port=SimpleNamespace(), lease=lease)
    token = current_credit_gate.set(gate_state)
    try:
        with bind_catalog_snapshot(snap):
            resp = await mw.awrap_model_call(_FakeRequest(primary), _handler_429)
    finally:
        current_credit_gate.reset(token)

    assert resp.result[0].content == "fallback-answer"
    assert seen_models_429 == ["claude-opus-5-5-oauth", "deepseek-flash"]
    assert lease_mock.await_count == 1
    assert lease.ceiling_credits == 500.0
    assert lease.grant_generation == 3
    assert persist_mock.await_count == 1
    assert persist_mock.await_args.args == ("run-resil-1", route_fp)


# ---------------------------------------------------------------------------
# 9. Real Policy + policy_refuses, snapshot RouteFingerprint, and resume verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_admission_enforces_real_policy_refuses_and_snapshot_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = {
        "schema": TURN_SNAPSHOT_SCHEMA,
        "models": {
            "gpt-6.2-sol-oauth": {
                "model_key": "gpt-6.2-sol-oauth",
                "provider": "codex-oauth",
                "model_id": "gpt-6.2-sol",
                "billing": "subscription",
                "bound_base_url": "https://api.example.com/v1",
                "key_fingerprint": "fp-orig",
            },
            "deepseek-flash": {
                "model_key": "deepseek-flash",
                "provider": "deepseek",
                "model_id": "deepseek-chat",
                "billing": "cash",
                "bound_base_url": "https://api.deepseek.com",
                "key_fingerprint": None,
            },
        },
        "variants": {},
    }

    # 1) Mutated key_fingerprint rejected as catalog_route_changed
    mutated_route_fn = create_autospec(
        uf.resolve_live_route_fingerprint,
        instance=False,
        return_value=RouteFingerprint(
            model_key="gpt-6.2-sol-oauth",
            provider="codex-oauth",
            model_id="gpt-6.2-sol",
            bound_base_url="https://api.example.com/v1",
            key_fingerprint="fp-rotated",
            billing="subscription",
        ),
    )
    res_mutated = await uf.acquire_fallback_candidate_live(
        subject_user_id="user-1",
        candidate_model="gpt-6.2-sol-oauth",
        run_id="run-1",
        snapshot=snap,
        policy=ALLOW_ALL,
        resolve_route_fn=mutated_route_fn,
    )
    assert res_mutated.admitted is False
    assert res_mutated.reason == "catalog_route_changed"

    # 2) Real Policy denying model via models.allowed is rejected by policy_refuses
    clean_route_fn = create_autospec(
        uf.resolve_live_route_fingerprint,
        instance=False,
        return_value=RouteFingerprint(
            model_key="gpt-6.2-sol-oauth",
            provider="codex-oauth",
            model_id="gpt-6.2-sol",
            bound_base_url="https://api.example.com/v1",
            key_fingerprint="fp-orig",
            billing="subscription",
        ),
    )
    restricted_models_policy: Policy = merge_policy(
        None,
        PolicyLayer(models=ModelsPolicy(allowed=["claude-opus-5-5-oauth"])),
    )
    res_model_denied = await uf.acquire_fallback_candidate_live(
        subject_user_id="user-1",
        candidate_model="gpt-6.2-sol-oauth",
        run_id="run-1",
        snapshot=snap,
        policy=restricted_models_policy,
        resolve_route_fn=clean_route_fn,
    )
    assert res_model_denied.admitted is False
    assert res_model_denied.reason == "policy_denied:models.allowed"

    # 3) Real Policy denying own_model_access.subscriptions is rejected by policy_refuses
    no_subs_policy: Policy = merge_policy(
        None,
        PolicyLayer(own_model_access=OwnModelAccessPolicy(subscriptions=False)),
    )
    res_sub_denied = await uf.acquire_fallback_candidate_live(
        subject_user_id="user-1",
        candidate_model="gpt-6.2-sol-oauth",
        run_id="run-1",
        snapshot=snap,
        policy=no_subs_policy,
        resolve_route_fn=clean_route_fn,
    )
    assert res_sub_denied.admitted is False
    assert res_sub_denied.reason == "policy_denied:own_model_access.subscriptions"

    # 4) Payer/subject mismatch rejected in Gate 3
    res_payer_mismatch = await uf.acquire_fallback_candidate_live(
        subject_user_id="user-1",
        payer_user_id="other-payer",
        candidate_model="gpt-6.2-sol-oauth",
        run_id="run-1",
        snapshot=snap,
        policy=ALLOW_ALL,
        resolve_route_fn=clean_route_fn,
    )
    assert res_payer_mismatch.admitted is False
    assert res_payer_mismatch.reason == "payer_subject_mismatch"

    # 5) Cash model rejected when allow_cash_fallback_on_unattended is False
    monkeypatch.delenv("DALTON_ALLOW_CASH_FALLBACK_ON_UNATTENDED", raising=False)
    cash_route_fn = create_autospec(
        uf.resolve_live_route_fingerprint,
        instance=False,
        return_value=RouteFingerprint(
            model_key="deepseek-flash",
            provider="deepseek",
            model_id="deepseek-chat",
            bound_base_url="https://api.deepseek.com",
            key_fingerprint=None,
            billing="cash",
        ),
    )
    res_cash_off = await uf.acquire_fallback_candidate_live(
        subject_user_id="user-1",
        candidate_model="deepseek-flash",
        run_id="run-1",
        snapshot=snap,
        policy=ALLOW_ALL,
        resolve_route_fn=cash_route_fn,
    )
    assert res_cash_off.admitted is False
    assert res_cash_off.reason == "cash_fallback_disabled"


@pytest.mark.asyncio
async def test_verify_resume_route_honors_active_fallback_route_and_snapshotted_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = {
        "schema": TURN_SNAPSHOT_SCHEMA,
        "models": {
            "claude-opus-5-5-oauth": {
                "model_key": "claude-opus-5-5-oauth",
                "provider": "anthropic-oauth",
                "model_id": "claude-opus-5-5",
                "billing": "subscription",
            },
            "gpt-6.2-sol-oauth": {
                "model_key": "gpt-6.2-sol-oauth",
                "provider": "codex-oauth",
                "model_id": "gpt-6.2-sol",
                "billing": "subscription",
                "bound_base_url": "https://codex.example.com",
                "key_fingerprint": "fp-1",
            },
        },
        "variants": {},
    }
    run_row = {
        "conversation_response_id": "run-1",
        "metadata": {
            "llm_model": "claude-opus-5-5-oauth",
            "active_fallback_route": {"model_key": "gpt-6.2-sol-oauth"},
            "effective_catalog_snapshot": snap,
        },
    }
    ok_route = RouteFingerprint(
        model_key="gpt-6.2-sol-oauth",
        provider="codex-oauth",
        model_id="gpt-6.2-sol",
        bound_base_url="https://codex.example.com",
        key_fingerprint="fp-1",
        billing="subscription",
    )
    await verify_resume_route("run-1", ok_route, run_row=run_row)

    bad_route = RouteFingerprint(
        model_key="gpt-6.2-sol-oauth",
        provider="codex-oauth",
        model_id="gpt-6.2-sol",
        bound_base_url="https://mutated.example.com",
        key_fingerprint="fp-1",
        billing="subscription",
    )
    ok_cand, diffs = await verify_candidate_route_against_snapshot(
        "gpt-6.2-sol-oauth", bad_route, snapshot=snap
    )
    assert ok_cand is False
    assert "bound_base_url" in diffs
    with pytest.raises(HTTPException) as exc_info:
        await verify_resume_route("run-1", bad_route, run_row=run_row)
    assert exc_info.value.status_code == 400

    # Also verify pre-dispatch switch on resume when active_fallback_route wasn't written yet
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    run_row_primary_only = {
        "conversation_response_id": "run-2",
        "metadata": {
            "llm_model": "claude-opus-5-5-oauth",
            "effective_catalog_snapshot": snap,
        },
    }
    ctx = uf.UnattendedTurnContext(
        is_unattended=True, subject_user_id="u1", source_kind="wakeup", run_id="run-2"
    )
    with uf.bind_unattended_turn_context(ctx):
        await verify_resume_route("run-2", ok_route, run_row=run_row_primary_only)
        with pytest.raises(HTTPException) as bad_switch_exc:
            await verify_resume_route("run-2", bad_route, run_row=run_row_primary_only)
        assert bad_switch_exc.value.status_code == 400


# ---------------------------------------------------------------------------
# 10. Cash lease idempotency for repeated (run_ref, call_id)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_cash_lease_tx_idempotent_on_same_call_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {}
    conn = _FakeConn(state)
    lock_calls: list[tuple[str, Any]] = []

    async def _fake_lock(c: Any, uid: str, b_day: Any) -> None:
        lock_calls.append((uid, b_day))

    async def _fake_cfg(c: Any, uid: str) -> dict[str, Any]:
        return {
            "user_id": uid,
            "daily_cap_usd": Decimal("1.00"),
            "default_task_cap_usd": Decimal("1.00"),
            "timezone": "Asia/Shanghai",
            "configured": True,
        }

    async def _fake_summary(c: Any, uid: str) -> dict[str, Any]:
        rows = []
        open_res = Decimal("0")
        if state.get("row") is not None:
            r = dict(state["row"])
            r["charge_usd"] = max(Decimal(str(r["reserved_usd"])), Decimal(str(r["observed_usd"])))
            rows.append(r)
            open_res = r["charge_usd"]
        return {
            "_raw_rows": rows,
            "_decimals": {
                "cap_usd": Decimal("1.00"),
                "spent_usd": open_res,
                "settled_usd": Decimal("0"),
                "unreserved_usage_usd": Decimal("0"),
                "open_reserved_usd": open_res,
                "expired_conservative_usd": Decimal("0"),
            },
            "configured": True,
        }

    async def _fake_ctx(c: Any, ref: str) -> tuple[None, None]:
        return None, None

    async def _fake_cap(c: Any, uid: str, tid: Any, wid: Any, default_cap: Any) -> Any:
        return default_cap

    monkeypatch.setattr(budget_port, "acquire_budget_advisory_lock", _fake_lock)
    monkeypatch.setattr(budget_port, "resolve_user_budget_config", _fake_cfg)
    monkeypatch.setattr(budget_port, "reconcile_and_summarize_day", _fake_summary)
    monkeypatch.setattr(budget_port, "lookup_run_context", _fake_ctx)
    monkeypatch.setattr(budget_port, "resolve_effective_task_cap", _fake_cap)

    v1 = await budget_port.acquire_cash_lease_tx(
        conn,
        user_id="user-1",
        run_ref="11111111-1111-1111-1111-111111111111",
        call_id="call-abc",
        min_floor_usd=Decimal("0.25"),
    )
    assert v1.granted is True
    reserved_after_first = state["row"]["reserved_usd"]
    gen_after_first = v1.generation
    assert len(lock_calls) == 1

    v2 = await budget_port.acquire_cash_lease_tx(
        conn,
        user_id="user-1",
        run_ref="11111111-1111-1111-1111-111111111111",
        call_id="call-abc",
        min_floor_usd=Decimal("0.25"),
    )
    assert v2.granted is True
    assert state["row"]["reserved_usd"] == reserved_after_first
    assert v2.generation == gen_after_first
    assert v2.ceiling_credits == v1.ceiling_credits


# ---------------------------------------------------------------------------
# 11. Error classification contrast: switches OFF vs interactive vs unattended ON
# ---------------------------------------------------------------------------


def test_classify_error_off_and_interactive_preserve_retryable_while_unattended_on_skips_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_429 = _RemoteQuotaExhaustedError(
        "Error code: 429 plan limit reached",
        429,
        provider_refusal=ProviderRefusal("plan_limit"),
    )

    # Contrast 1: BOTH switches OFF -> terminal 429 stays recoverable (identical to upstream!)
    monkeypatch.delenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", raising=False)
    monkeypatch.delenv("DALTON_ALLOW_CASH_FALLBACK_ON_UNATTENDED", raising=False)
    res_off_default = classify_error(remote_429)
    assert res_off_default["is_recoverable"] is True
    assert res_off_default["error_type"] == "api_error"
    res_off_flag_true = classify_error(remote_429, unattended_turn=True)
    assert res_off_flag_true["is_recoverable"] is True

    # Contrast 2: Main switch ON, interactive turn (unattended_turn=False) -> stays recoverable!
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    assert classify_error(remote_429, unattended_turn=False)["is_recoverable"] is True

    # Contrast 3: Main switch ON AND trusted unattended turn -> skips outer same-model auto-retry!
    assert classify_error(remote_429, unattended_turn=True)["is_recoverable"] is False
    reauth_exc = HTTPException(
        status_code=400,
        detail={"type": "oauth_reauth_required", "message": "Reconnect"},
    )
    assert classify_error(reauth_exc, unattended_turn=True)["is_recoverable"] is False

    # Transient 502 remains recoverable even when switch ON and unattended
    transient_502 = RuntimeError("Error code: 502 Bad Gateway")
    assert classify_error(transient_502, unattended_turn=True)["is_recoverable"] is True


# ---------------------------------------------------------------------------
# 12. Internal LLM host allowlist: system catalog enforced + structured trigger only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internal_llm_complete_system_only_and_structured_trigger_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton import access_tiers
    from src.dalton.app import internal_llm_router as router_mod
    from src.llms.llm import LLM as LLMFactory

    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    monkeypatch.setattr(
        router_mod,
        "get_allowed_host_oauth_models",
        lambda: frozenset({"claude-opus-5-5-oauth", "gpt-6.2-sol-oauth"}),
    )
    monkeypatch.setattr(
        access_tiers,
        "get_primary_admin_user_id",
        create_autospec(access_tiers.get_primary_admin_user_id, instance=False, return_value="admin-1"),
    )

    fake_mc = SimpleNamespace(
        get_model_config=lambda m: {"provider": "oauth", "model_id": m}
        if m in ("claude-opus-5-5-oauth", "gpt-6.2-sol-oauth")
        else None
    )
    monkeypatch.setattr(LLMFactory, "get_model_config", lambda: fake_mc)

    async def _avail_ok(
        user_id: str,
        model_name: str,
        *,
        client: Any = None,
        prefs: Any = None,
        ignore_custom_models: bool = False,
        now: Any = None,
    ) -> uf.SubscriptionAvailability:
        assert ignore_custom_models is True
        return uf.SubscriptionAvailability(available=True, provider="oauth", model_name=model_name)

    monkeypatch.setattr(uf, "inspect_subscription_availability", _avail_ok)
    monkeypatch.setattr(
        uf,
        "_classify_candidate_billing",
        lambda model_name, prefs=None, ignore_custom_models=False: ("oauth", "oauth"),
    )

    # Case A: Transient 429 / text containing "billing" WITHOUT structured trigger MUST NOT re-send
    transient_calls: list[dict[str, Any]] = []
    transient_complete = create_autospec(complete_mod.dalton_complete, instance=False)

    async def _transient_side_effect(*args: Any, **kwargs: Any) -> complete_mod.DaltonCompleteResult:
        transient_calls.append(dict(kwargs))
        return complete_mod.DaltonCompleteResult(
            ok=False,
            status="indeterminate",
            model=str(kwargs["model"]),
            error="HTTP 429 rate_limit billing text",
            error_kind="rate_limit",
            refusal_reason="rate_limited",
            quota_or_auth_trigger=False,
        )

    transient_complete.side_effect = _transient_side_effect
    monkeypatch.setattr(router_mod, "dalton_complete", transient_complete)

    req = router_mod.InternalLLMCompleteRequest(
        model="claude-opus-5-5-oauth",
        user_prompt="summarize",
        purpose="podcast_weekly",
    )
    resp_transient = await router_mod.internal_llm_complete(req)
    assert resp_transient.ok is False
    assert len(transient_calls) == 1
    assert transient_calls[0]["ignore_custom_models"] is True

    # Case B: Structured terminal quota refusal (quota_or_auth_trigger=True) switches within allowlist
    structured_calls: list[dict[str, Any]] = []
    structured_complete = create_autospec(complete_mod.dalton_complete, instance=False)

    async def _structured_side_effect(*args: Any, **kwargs: Any) -> complete_mod.DaltonCompleteResult:
        structured_calls.append(dict(kwargs))
        if kwargs["model"] == "claude-opus-5-5-oauth":
            return complete_mod.DaltonCompleteResult(
                ok=False,
                status="failed",
                model=str(kwargs["model"]),
                error="plan limit reached",
                error_kind="http_429",
                refusal_reason="plan_limit",
                quota_or_auth_trigger=True,
            )
        return complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            text="fallback-ok",
            model=str(kwargs["model"]),
        )

    structured_complete.side_effect = _structured_side_effect
    monkeypatch.setattr(router_mod, "dalton_complete", structured_complete)

    resp_ok = await router_mod.internal_llm_complete(req)
    assert resp_ok.ok is True
    assert resp_ok.model == "gpt-6.2-sol-oauth"
    assert [(c["user_id"], c["model"], c["ignore_custom_models"]) for c in structured_calls] == [
        ("admin-1", "claude-opus-5-5-oauth", True),
        ("admin-1", "gpt-6.2-sol-oauth", True),
    ]


# ---------------------------------------------------------------------------
# 13. assist_json default-chain structured trigger only when enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assist_json_default_chain_structured_trigger_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        assist_mod,
        "resolve_assist_model",
        create_autospec(assist_mod.resolve_assist_model, instance=False, return_value="claude-opus-5-5-oauth"),
    )
    monkeypatch.setattr(
        assist_mod,
        "resolve_assist_fallback_models",
        create_autospec(assist_mod.resolve_assist_fallback_models, instance=False, return_value=["gpt-6.2-sol-oauth"]),
    )
    monkeypatch.setattr(
        complete_mod,
        "resolve_model_billing_and_provider",
        lambda model_key: ("oauth", "oauth"),
    )

    calls: list[str] = []
    complete_mock = create_autospec(complete_mod.dalton_complete, instance=False)

    async def _side_effect(*args: Any, **kwargs: Any) -> complete_mod.DaltonCompleteResult:
        m = str(kwargs["request_model"])
        calls.append(m)
        if m == "claude-opus-5-5-oauth":
            return complete_mod.DaltonCompleteResult(
                ok=False,
                status="failed",
                model=m,
                error="plan_limit",
                error_kind="http_429",
                refusal_reason="plan_limit",
                quota_or_auth_trigger=True,
            )
        return complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            data={"answer": "ok"},
            text='{"answer": "ok"}',
            model=m,
        )

    complete_mock.side_effect = _side_effect
    monkeypatch.setattr(assist_mod, "dalton_complete", complete_mock)

    # Disabled: does not switch on default chain
    monkeypatch.delenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", raising=False)
    res_off = await assist_mod.assist_json(
        task="test.extract",
        instructions="return json",
        payload={"x": 1},
        schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        user_id="user-1",
    )
    assert res_off.ok is False
    assert calls == ["claude-opus-5-5-oauth"]

    # Enabled: switches on structured quota trigger
    calls.clear()
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    res_on = await assist_mod.assist_json(
        task="test.extract",
        instructions="return json",
        payload={"x": 1},
        schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        user_id="user-1",
    )
    assert res_on.ok is True
    assert res_on.model == "gpt-6.2-sol-oauth"
    assert calls == ["claude-opus-5-5-oauth", "gpt-6.2-sol-oauth"]


@pytest.mark.asyncio
async def test_item3_failure_notice_call_site_noops_when_absent() -> None:
    sent = await uf._maybe_enqueue_failure_notice_in_tx(
        None,
        subject_kind="wakeup",
        subject_id=str(uuid4()),
        source_key="wake-1",
        recipient_user_id="owner-1",
        failure_reason="subscription_quota_exhausted",
        summary={"message": "quota exhausted"},
    )
    assert sent is False
