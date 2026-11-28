"""Unit tests for Prod Audit 2 §2: unattended-turn subscription quota fallback.

Covers:
- Default-off switches ``is_unattended_quota_fallback_enabled`` & ``allow_cash_fallback_on_unattended``.
- Migration ``d062_wakeup_quota_deferrals`` single head, no ``src.*`` imports, upgrade/downgrade SQL.
- Trusted server-side unattended source classification (``posted_by_server``, ``classify_origin``,
  ``run_unwatched``, CoS handoff, internal host LLM, ``assist_json``; client spoofing rejected).
- Structured Redis quota cooldown & OAuth health probing (``inspect_subscription_availability``).
- Profile-aware candidate chain resolution & default ``$0`` OAuth pool isolation.
- ``ModelResilienceMiddleware`` ``quota_or_auth_only`` policy: transient 502 stays on primary;
  quota cooldown / terminal 429 triggers live 4-gate admission and switches only if admitted.
- Live 4-gate admission checks (modality, custom-model isolation, snapshot RouteFingerprint,
  fresh org policy, cash-fallback switch, platform credit, atomic cash lease idempotency).
- Terminal quota refusal skipping outer 3x auto-retry in ``error_handling.classify_error``.
- ``/internal/llm/complete`` allowlisted OAuth fallback under same ``primary_admin_id``.
- ``assist_json`` default-chain quota 429 fallback when enabled, unchanged when disabled.
- Optional Item 3 failure-notice call-site clean no-op when module absent or switch off.
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
from unittest.mock import AsyncMock, create_autospec

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from langchain_core.messages import AIMessage

from src.dalton.budget import port as budget_port
from src.dalton.llm import assist as assist_mod
from src.dalton.llm import unattended_fallback as uf
from src.dalton.services.team_model_catalog.constants import TURN_SNAPSHOT_SCHEMA
from src.dalton.services.team_model_catalog.snapshot import (
    RouteFingerprint,
    verify_candidate_route_against_snapshot,
    verify_resume_route,
)
from src.dalton.settings import (
    allow_cash_fallback_on_unattended,
    is_unattended_quota_fallback_enabled,
)
from src.llms.provider_errors import ProviderRefusal
from src.ptc_agent.agent.middleware.model_resilience import (
    ModelResilienceMiddleware,
    _RemoteQuotaExhaustedError,
)
from src.server.handlers.chat.error_handling import classify_error

REPO_ROOT = Path(__file__).resolve().parents[3]
D062_PATH = REPO_ROOT / "src" / "dalton" / "migrations" / "versions" / "d062_wakeup_quota_deferrals.py"


class _FakeModel:
    """Minimal chat-model test double with explicit call metadata."""

    def __init__(self, name: str, billing_type: str = "oauth") -> None:
        self.model = name
        self._call_metadata = {
            "catalog_model": name,
            "manifest_model": name,
            "billing_type": billing_type,
            "pricing_provider": "anthropic-oauth" if billing_type == "oauth" else "deepseek",
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
# 2. Trusted server-side unattended source detection
# ---------------------------------------------------------------------------


def test_trusted_unattended_source_classification() -> None:
    # Automation run metadata
    assert uf.is_unattended_turn(run_metadata={"automation_execution_id": "exec-1"}) is True
    assert uf.is_unattended_turn(run_metadata={"automation_id": "auto-1"}) is True
    assert uf.is_unattended_turn(SimpleNamespace(origin=SimpleNamespace(type="automation"))) is True

    # Server-posted wakeup / market_target / system report-back (posted_by_server)
    assert uf.is_unattended_turn(query_type="wakeup", platform=None, from_service=True) is True
    assert uf.is_unattended_turn(query_type="market_target", platform=None, from_service=True) is True
    assert uf.is_unattended_turn(query_type="system", platform=None, from_service=True) is True

    # Unwatched restart-resume run & CoS handoff & internal/assist
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
# 3. Structured Redis cooldown probe & candidate chain ($0 OAuth default)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_subscription_availability_reads_redis_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
    reset_dt = now + timedelta(hours=2)

    class _FakeCache:
        def __init__(self, payload: dict[str, Any] | None) -> None:
            self._payload = payload

        async def get(self, key: str) -> Any:
            return self._payload

    monkeypatch.setattr(
        uf,
        "_resolve_model_subscription_info",
        lambda m, **kw: ("anthropic-oauth", "anthropic-oauth", m, False),
    )
    monkeypatch.setattr(
        uf,
        "_compute_cooldown_redis_key_without_client",
        lambda **kw: "dalton:quota_exhausted:holder:anthropic-oauth:model:claude-opus-5-5-oauth",
    )

    from src.utils.cache import redis_cache

    # Active cooldown with known until
    cache = _FakeCache(
        {
            "expires_at": reset_dt.timestamp(),
            "refusal": {"reason": "plan_limit", "until": reset_dt.isoformat()},
        }
    )
    monkeypatch.setattr(redis_cache, "get_cache_client", lambda: cache)
    avail = await uf.inspect_subscription_availability("u1", "claude-opus-5-5-oauth", now=now)
    assert avail.available is False
    assert avail.kind == "quota_cooldown"
    assert avail.refusal_code == "plan_limit"
    assert avail.reset_at == reset_dt
    assert avail.cooldown_key == "dalton:quota_exhausted:holder:anthropic-oauth:model:claude-opus-5-5-oauth"

    # Active cooldown with unknown reset (plan_limit without until)
    cache_unknown = _FakeCache(
        {
            "expires_at": (now + timedelta(hours=1)).timestamp(),
            "refusal": {"reason": "plan_limit"},
        }
    )
    monkeypatch.setattr(redis_cache, "get_cache_client", lambda: cache_unknown)
    avail_unknown = await uf.inspect_subscription_availability("u1", "claude-opus-5-5-oauth", now=now)
    assert avail_unknown.available is False
    assert avail_unknown.kind == "quota_cooldown"
    assert avail_unknown.reset_at is None

    # Expired cooldown -> available
    cache_expired = _FakeCache({"expires_at": (now - timedelta(minutes=1)).timestamp()})
    monkeypatch.setattr(redis_cache, "get_cache_client", lambda: cache_expired)
    avail_ok = await uf.inspect_subscription_availability("u1", "claude-opus-5-5-oauth", now=now)
    assert avail_ok.available is True


def test_resolve_unattended_fallback_chain_defaults_to_zero_dollar_oauth_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DALTON_ALLOW_CASH_FALLBACK_ON_UNATTENDED", raising=False)
    billing_map = {
        "claude-opus-5-5-oauth": ("oauth", "anthropic-oauth"),
        "gpt-6.2-sol-oauth": ("oauth", "codex-oauth"),
        "gemini-4-neon-high-oauth": ("oauth", "antigravity-oauth"),
        "gemini-4-carbon-medium-oauth": ("oauth", "antigravity-oauth"),
        "deepseek-flash": ("cash", "deepseek"),
    }
    monkeypatch.setattr(
        uf,
        "_classify_candidate_billing",
        lambda m, **kw: billing_map.get(m, ("cash", "unknown")),
    )
    monkeypatch.setattr(
        uf,
        "get_profile_default_fallback_chain",
        lambda: ["gpt-6.2-sol-oauth", "deepseek-flash", "gemini-4-neon-high-oauth"],
    )

    # Default allow_cash=False filters out deepseek-flash (cash), keeps $0 OAuth models
    chain = uf.resolve_unattended_fallback_chain(
        user_prefs={"fallback_models": []},  # empty list falls through unless disable_fallback=True
        primary_model="claude-opus-5-5-oauth",
    )
    assert chain == ["gpt-6.2-sol-oauth", "gemini-4-neon-high-oauth"]

    # Explicit disable_fallback=True returns []
    assert (
        uf.resolve_unattended_fallback_chain(
            user_prefs={"disable_fallback": True, "fallback_models": ["gpt-6.2-sol-oauth"]},
            primary_model="claude-opus-5-5-oauth",
        )
        == []
    )

    # With allow_cash=True, cash model is included
    chain_cash = uf.resolve_unattended_fallback_chain(
        user_prefs={},
        primary_model="claude-opus-5-5-oauth",
        allow_cash=True,
    )
    assert "deepseek-flash" in chain_cash


# ---------------------------------------------------------------------------
# 4. ModelResilienceMiddleware: transient 502 stays on primary; quota 429 switches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resilience_transient_502_stays_on_primary_quota_429_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _FakeModel("claude-opus-5-5-oauth", billing_type="oauth")
    fallback = _FakeModel("gpt-6.2-sol-oauth", billing_type="oauth")
    uf.stamp_quota_or_auth_only(
        fallback,
        candidate_name="gpt-6.2-sol-oauth",
        subject_user_id="user-1",
        run_id="run-1",
    )

    # Stub live admission to succeed
    live_mock = create_autospec(uf.acquire_fallback_candidate_live, instance=False)
    live_mock.return_value = uf.FallbackAdmissionResult(
        admitted=True, model_name="gpt-6.2-sol-oauth", billing="oauth"
    )
    monkeypatch.setattr(uf, "acquire_fallback_candidate_live", live_mock)

    mw = ModelResilienceMiddleware(
        primary_name="claude-opus-5-5-oauth",
        primary_client=primary,
        fallbacks=[("gpt-6.2-sol-oauth", fallback)],
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
    assert live_mock.await_count == 0

    # Case B: Terminal quota 429 (plan_limit) switches to fallback after live admission
    seen_models_429: list[str] = []

    async def _handler_429(req: Any) -> Any:
        seen_models_429.append(req.model.model)
        if req.model.model == "claude-opus-5-5-oauth":
            exc = _RemoteQuotaExhaustedError(
                "Plan limit reached",
                429,
                provider_refusal=ProviderRefusal("plan_limit"),
            )
            raise exc
        return _FakeResponse("fallback-answer")

    resp = await mw.awrap_model_call(_FakeRequest(primary), _handler_429)
    assert resp.result[0].content == "fallback-answer"
    assert seen_models_429 == ["claude-opus-5-5-oauth", "gpt-6.2-sol-oauth"]
    assert live_mock.await_count == 1


# ---------------------------------------------------------------------------
# 5. Live 4-gate admission & snapshot RouteFingerprint consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_admission_rejects_mutated_route_policy_and_disabled_cash(
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

    # Mutated key_fingerprint rejected as catalog_route_changed
    async def _mutated_route(mk: str, **kw: Any) -> RouteFingerprint:
        return RouteFingerprint(
            model_key=mk,
            provider="codex-oauth",
            model_id="gpt-6.2-sol",
            bound_base_url="https://api.example.com/v1",
            key_fingerprint="fp-rotated",
            billing="subscription",
        )

    res_mutated = await uf.acquire_fallback_candidate_live(
        subject_user_id="user-1",
        candidate_model="gpt-6.2-sol-oauth",
        run_id="run-1",
        snapshot=snap,
        policy=SimpleNamespace(models=SimpleNamespace(allowed=None, denied=None)),
        resolve_route_fn=_mutated_route,
    )
    assert res_mutated.admitted is False
    assert res_mutated.reason == "catalog_route_changed"

    # Cash model rejected when allow_cash_fallback_on_unattended is False
    monkeypatch.delenv("DALTON_ALLOW_CASH_FALLBACK_ON_UNATTENDED", raising=False)

    async def _cash_route(mk: str, **kw: Any) -> RouteFingerprint:
        return RouteFingerprint(
            model_key=mk,
            provider="deepseek",
            model_id="deepseek-chat",
            bound_base_url="https://api.deepseek.com",
            key_fingerprint=None,
            billing="cash",
        )

    res_cash_off = await uf.acquire_fallback_candidate_live(
        subject_user_id="user-1",
        candidate_model="deepseek-flash",
        run_id="run-1",
        snapshot=snap,
        policy=SimpleNamespace(models=SimpleNamespace(allowed=None, denied=None)),
        resolve_route_fn=_cash_route,
    )
    assert res_cash_off.admitted is False
    assert res_cash_off.reason == "cash_fallback_disabled"

    # Org policy deny blocks candidate
    async def _clean_route(mk: str, **kw: Any) -> RouteFingerprint:
        return RouteFingerprint(
            model_key=mk,
            provider="codex-oauth",
            model_id="gpt-6.2-sol",
            bound_base_url="https://api.example.com/v1",
            key_fingerprint="fp-orig",
            billing="subscription",
        )

    res_policy = await uf.acquire_fallback_candidate_live(
        subject_user_id="user-1",
        candidate_model="gpt-6.2-sol-oauth",
        run_id="run-1",
        snapshot=snap,
        policy=SimpleNamespace(models=SimpleNamespace(allowed=["claude-opus-5-5-oauth"], denied=None)),
        resolve_route_fn=_clean_route,
    )
    assert res_policy.admitted is False
    assert res_policy.reason == "policy_model_not_allowed"


@pytest.mark.asyncio
async def test_verify_resume_route_honors_active_fallback_route() -> None:
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


# ---------------------------------------------------------------------------
# 6. Cash lease idempotency for repeated (run_ref, call_id)
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

    # Second identical (run_ref, call_id) returns same verdict with ZERO extra deduction
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
# 7. Terminal quota refusal skips outer auto-retry
# ---------------------------------------------------------------------------


def test_classify_error_skips_outer_retry_on_terminal_quota_refusal() -> None:
    remote_exc = _RemoteQuotaExhaustedError(
        "Error code: 429 plan limit reached",
        429,
        provider_refusal=ProviderRefusal("plan_limit"),
    )
    res_quota = classify_error(remote_exc)
    assert res_quota["is_recoverable"] is False

    reauth_exc = HTTPException(
        status_code=400,
        detail={"type": "oauth_reauth_required", "message": "Reconnect"},
    )
    assert classify_error(reauth_exc)["is_recoverable"] is False

    # Transient 502 remains recoverable as before
    transient_502 = RuntimeError("Error code: 502 Bad Gateway")
    assert classify_error(transient_502)["is_recoverable"] is True


# ---------------------------------------------------------------------------
# 8. Internal LLM host allowlist fallback & assist_json default-chain fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internal_llm_complete_falls_back_in_allowlist_same_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.app import internal_llm_router as router_mod

    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    monkeypatch.setattr(
        router_mod,
        "get_allowed_host_oauth_models",
        lambda: frozenset({"claude-opus-5-5-oauth", "gpt-6.2-sol-oauth"}),
    )
    from src.dalton import access_tiers

    async def _fake_admin() -> str:
        return "admin-1"

    monkeypatch.setattr(access_tiers, "get_primary_admin_user_id", _fake_admin)
    monkeypatch.setattr(
        uf,
        "inspect_subscription_availability",
        AsyncMock(
            side_effect=lambda uid, m, **kw: uf.SubscriptionAvailability(
                available=True, provider="oauth", model_name=m
            )
        ),
    )
    monkeypatch.setattr(
        uf,
        "_classify_candidate_billing",
        lambda m, **kw: ("oauth", "oauth"),
    )

    calls: list[tuple[str, str]] = []

    async def _fake_complete(**kw: Any) -> Any:
        calls.append((kw["user_id"], kw["model"]))
        if kw["model"] == "claude-opus-5-5-oauth":
            return SimpleNamespace(
                ok=False, text=None, data=None, model=kw["model"], status="failed",
                error="429 plan_limit", error_kind="http_429",
            )
        return SimpleNamespace(
            ok=True, text="fallback-ok", data=None, model=kw["model"], status="done",
            error=None, error_kind=None,
        )

    monkeypatch.setattr(router_mod, "dalton_complete", _fake_complete)

    req = router_mod.InternalLLMCompleteRequest(
        model="claude-opus-5-5-oauth",
        user_prompt="summarize",
        purpose="podcast_weekly",
    )
    resp = await router_mod.internal_llm_complete(req)
    assert resp.ok is True
    assert resp.model == "gpt-6.2-sol-oauth"
    assert calls == [
        ("admin-1", "claude-opus-5-5-oauth"),
        ("admin-1", "gpt-6.2-sol-oauth"),
    ]


@pytest.mark.asyncio
async def test_assist_json_default_chain_switches_on_429_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_resolve_model(**kw: Any) -> str:
        return "claude-opus-5-5-oauth"

    async def _fake_resolve_fb(**kw: Any) -> list[str]:
        return ["gpt-6.2-sol-oauth"]

    monkeypatch.setattr(assist_mod, "resolve_assist_model", _fake_resolve_model)
    monkeypatch.setattr(assist_mod, "resolve_assist_fallback_models", _fake_resolve_fb)

    from src.dalton.llm import complete as complete_mod

    monkeypatch.setattr(
        complete_mod,
        "resolve_model_billing_and_provider",
        lambda m: ("oauth", "oauth"),
    )

    calls: list[str] = []

    async def _fake_complete(**kw: Any) -> Any:
        m = kw["request_model"]
        calls.append(m)
        if m == "claude-opus-5-5-oauth":
            return SimpleNamespace(
                ok=False, data=None, text=None, model=m, cash_usd=0.0,
                status="failed", error="429 quota_exceeded", error_kind="http_429",
                usage={},
            )
        return SimpleNamespace(
            ok=True, data={"answer": "ok"}, text='{"answer": "ok"}', model=m, cash_usd=0.0,
            status="done", error=None, error_kind=None, usage={},
        )

    monkeypatch.setattr(assist_mod, "dalton_complete", _fake_complete)

    # Disabled: does not switch on default chain 429
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

    # Enabled: switches to fallback on 429
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
        subject_id="root-1",
        source_key="wake-1",
        recipient_user_id="owner-1",
        failure_reason="subscription_quota_exhausted",
        summary={"message": "quota exhausted"},
    )
    assert sent is False
