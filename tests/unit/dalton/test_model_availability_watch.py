"""Unit tests for daily subscription model availability watch (T87, offline)."""

from __future__ import annotations

import inspect
import stat
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.dalton import researchdata_bridge as rb
from scripts.dalton.ops import model_availability_watch as watch_cli
from src.dalton.app.internal_model_availability_router import (
    internal_model_availability_router,
)
from src.dalton.data_jobs.manifest import load_data_task_manifest
from src.dalton.services import model_availability_watch as watch_svc
from src.llms.llm import LLM, ModelConfig
from src.server.services.llm import egress
from src.server.services.llm.provider_models import service as pm_service
from src.server.services.llm.provider_models import wire as pm_wire


def test_t87_manifest_entry_and_bridge_acceptance(tmp_path: Path) -> None:
    manifest = load_data_task_manifest()
    by_code = manifest.by_task_code()
    by_key = manifest.by_job_key()
    assert "T87" in by_code
    entry = by_code["T87"]
    assert entry is by_key["model-availability-watch"]
    assert entry.runner == "host"
    assert entry.host_mode == "run"
    assert entry.cron == "15 9 * * *"
    assert entry.timezone == "Asia/Hong_Kong"
    assert entry.timeout_seconds == 300
    assert entry.no_output_timeout_seconds == 240
    assert entry.enabled is False
    assert entry.notify_enabled is False
    assert entry.secret_names == ["INTERNAL_SERVICE_TOKEN"]
    assert entry.env["DALTON_BACKEND_URL"] == "http://127.0.0.1:8000"
    assert inspect.signature(watch_cli.fetch_backend_check).parameters["timeout_s"].default == 180.0

    spec = rb.resolve_host_run_command_spec(
        job_key="model-availability-watch",
        command_argv=list(entry.command_argv),
        cwd=entry.cwd,
        data_root=tmp_path,
        ocw_root=tmp_path,
    )
    assert spec["command_argv"] == list(entry.command_argv)
    assert spec["cwd"] == str(tmp_path)

    # Resolve the relative script path from the host runner's configured cwd against REPO_ROOT.
    repo_root = Path(__file__).resolve().parents[3]
    resolved_script = (repo_root / entry.cwd / spec["command_argv"][1]).resolve()
    assert resolved_script == (repo_root / "scripts/dalton/ops/model_availability_watch.py").resolve()
    assert resolved_script.is_file()


@pytest.mark.asyncio
async def test_check_flags_missing_visible_model_and_dedups_24h(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    LLM._model_config = None
    mc = ModelConfig()
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-holder-1,pm-2")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")

    async def _fake_available_models(
        user_id: str,
        provider: str,
        *,
        refresh: bool = False,
        q: str | None = None,
    ) -> dict[str, Any]:
        assert user_id == "pm-holder-1"
        assert refresh is True
        if provider == "codex-oauth":
            return {
                "provider": "codex-oauth",
                "status": "ok",
                "fetched_at": "2026-11-18T01:15:00Z",
                "ready": ["gpt-6.2-astra-oauth"],
                "missing": ["gpt-6.2-sol-oauth"],
                "context_windows": {},
                "others": [],
                "total": 1,
                "cached": False,
            }
        if provider == "claude-oauth":
            return {
                "provider": "claude-oauth",
                "status": "ok",
                "fetched_at": "2026-11-18T01:15:00Z",
                "ready": ["claude-opus-5-5-oauth"],
                "missing": [],
                "context_windows": {},
                "others": [],
                "total": 1,
                "cached": False,
            }
        raise pm_service.NotConnected(provider)

    avail_spy = create_autospec(pm_service.available_models, side_effect=_fake_available_models)
    monkeypatch.setattr(pm_service, "available_models", avail_spy)
    monkeypatch.setattr(
        pm_service,
        "_resolve_credential",
        create_autospec(
            pm_service._resolve_credential,
            side_effect=pm_service.NotConnected("antigravity-oauth"),
        ),
    )
    monkeypatch.setattr(LLM, "get_model_config", create_autospec(LLM.get_model_config, return_value=mc))
    npm_spy = create_autospec(watch_svc.fetch_npm_codex_latest_version, return_value=None)

    report = await watch_svc.run_model_availability_check(npm_version_fetcher=npm_spy)
    assert report["ok"] is True
    assert report["user_id"] == "pm-holder-1"
    assert report["missing_by_provider"] == {"codex-oauth": ["gpt-6.2-sol-oauth"]}
    assert {c.args[1] for c in avail_spy.await_args_list} == {"codex-oauth", "claude-oauth"}
    assert report["providers"]["antigravity-oauth"]["status"] == "not_connected"
    assert report["providers"]["antigravity-oauth"]["supported"] is True
    assert report["providers"]["xai-oauth"]["status"] == "unsupported"
    assert report["providers"]["muse-oauth"]["status"] == "unsupported"
    assert report["codex_version_probe"]["status"] == "skipped"
    assert report["codex_version_probe"]["reason"] == "npm_version_unavailable"

    sent_details: list[str] = []

    def _fake_sender(detail: str) -> tuple[bool, int | None, str | None]:
        sent_details.append(detail)
        return True, 200, None

    sender_spy = create_autospec(_fake_sender, side_effect=_fake_sender)
    state_dir = tmp_path / "state"
    t0 = 1_763_428_500.0

    # 1st run: sends alert and records 24h dedup state + latest.json (mode 0600).
    s1, rc1 = watch_cli.run_watch(
        state_dir=state_dir,
        check_report=report,
        alert_sender=sender_spy,
        now_epoch=t0,
    )
    assert rc1 == 0
    assert s1["alert"]["needed"] is True
    assert s1["alert"]["sent"] is True
    assert s1["alert"]["suppressed_24h"] is False
    assert len(sent_details) == 1
    assert "codex-oauth" in sent_details[0]
    assert "gpt-6.2-sol-oauth" in sent_details[0]
    assert stat.S_IMODE((state_dir / "alert_state.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((state_dir / "latest.json").stat().st_mode) == 0o600

    # 2nd run within 24h with identical missing set: suppressed, no duplicate alert.
    s2, rc2 = watch_cli.run_watch(
        state_dir=state_dir,
        check_report=report,
        alert_sender=sender_spy,
        now_epoch=t0 + 12 * 3600,
    )
    assert rc2 == 0
    assert s2["alert"]["sent"] is False
    assert s2["alert"]["suppressed_24h"] is True
    assert len(sent_details) == 1

    # 3rd run after 24h: alerts again.
    s3, rc3 = watch_cli.run_watch(
        state_dir=state_dir,
        check_report=report,
        alert_sender=sender_spy,
        now_epoch=t0 + 24 * 3600 + 1,
    )
    assert rc3 == 0
    assert s3["alert"]["sent"] is True
    assert s3["alert"]["suppressed_24h"] is False
    assert len(sent_details) == 2

    # Different missing set within 24h: alerts immediately.
    changed_report = {
        **report,
        "missing_by_provider": {"codex-oauth": ["gpt-6.2-astra-oauth", "gpt-6.2-sol-oauth"]},
    }
    s4, rc4 = watch_cli.run_watch(
        state_dir=state_dir,
        check_report=changed_report,
        alert_sender=sender_spy,
        now_epoch=t0 + 24 * 3600 + 60,
    )
    assert rc4 == 0
    assert s4["alert"]["sent"] is True
    assert len(sent_details) == 3


@pytest.mark.asyncio
async def test_codex_higher_version_probe_unlocks_configured_model_and_hints_upgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    LLM._model_config = None
    mc = ModelConfig()
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-holder-1")
    monkeypatch.setenv("DALTON_CODEX_CLIENT_VERSION", "0.164.0")

    async def _fake_available_models(
        user_id: str,
        provider: str,
        *,
        refresh: bool = False,
        q: str | None = None,
    ) -> dict[str, Any]:
        if provider == "codex-oauth":
            return {
                "provider": "codex-oauth",
                "status": "ok",
                "fetched_at": "2026-11-18T01:15:00Z",
                "ready": ["gpt-6.1-sol-oauth"],
                "missing": ["gpt-6.2-astra-oauth", "gpt-6.2-sol-oauth"],
                "context_windows": {},
                "others": [],
                "total": 1,
                "cached": False,
            }
        raise pm_service.NotConnected(provider)

    monkeypatch.setattr(
        pm_service,
        "available_models",
        create_autospec(pm_service.available_models, side_effect=_fake_available_models),
    )
    monkeypatch.setattr(LLM, "get_model_config", create_autospec(LLM.get_model_config, return_value=mc))

    # Exercise real probe_codex_with_client_version wiring (URL client_version + User-Agent)
    seen_requests: list[pm_wire.ListRequest] = []

    async def _fake_resolve_cred(user_id: str, provider: str, _mc: Any) -> pm_service.Credential:
        if provider != "codex-oauth":
            raise pm_service.NotConnected(provider)
        return pm_service.Credential(
            api_key="oauth-tok",
            holder="codex-oauth",
            account_id="acct-1",
            plan_type="pro",
            token_headers={"ChatGPT-Account-Id": "acct-1"},
        )

    async def _fake_fetch_list(req: pm_wire.ListRequest, *, transport: Any = None, **kwargs: Any) -> dict[str, Any]:
        seen_requests.append(req)
        return {
            "models": [
                {"slug": "gpt-6.1-sol", "visibility": "list", "supported_in_api": True},
                {"slug": "gpt-6.2-sol", "visibility": "list", "supported_in_api": True},
                {"slug": "gpt-6.2-astra", "visibility": "list", "supported_in_api": True},
            ]
        }

    monkeypatch.setattr(
        pm_service,
        "_resolve_credential",
        create_autospec(pm_service._resolve_credential, side_effect=_fake_resolve_cred),
    )
    monkeypatch.setattr(
        egress,
        "user_endpoint_transport",
        create_autospec(egress.user_endpoint_transport, return_value=None),
    )
    monkeypatch.setattr(
        pm_wire,
        "fetch_list",
        create_autospec(pm_wire.fetch_list, side_effect=_fake_fetch_list),
    )

    npm_spy = create_autospec(watch_svc.fetch_npm_codex_latest_version, return_value="0.175.0")
    report = await watch_svc.run_model_availability_check(npm_version_fetcher=npm_spy)

    assert len(seen_requests) == 1
    assert "client_version=0.175.0" in seen_requests[0].url
    assert seen_requests[0].headers["User-Agent"] == "codex_cli_rs/0.175.0"

    probe = report["codex_version_probe"]
    assert probe["status"] == "ok"
    assert probe["current_version"] == "0.164.0"
    assert probe["npm_latest_version"] == "0.175.0"
    assert probe["unlocked_models"] == ["gpt-6.2-astra-oauth", "gpt-6.2-sol-oauth"]
    assert probe["upgrade_hint"] == "需要提高 DALTON_CODEX_CLIENT_VERSION 到 0.175.0"

    sent_details: list[str] = []
    sender_spy = create_autospec(
        lambda d: (True, 200, None),
        side_effect=lambda d: (sent_details.append(d) or (True, 200, None)),
    )
    summary, rc = watch_cli.run_watch(
        state_dir=tmp_path / "state",
        check_report=report,
        alert_sender=sender_spy,
        now_epoch=1_763_428_500.0,
    )
    assert rc == 0
    assert summary["alert"]["sent"] is True
    assert "需要提高 DALTON_CODEX_CLIENT_VERSION 到 0.175.0" in sent_details[0]
    assert "gpt-6.2-sol-oauth" in sent_details[0]


@pytest.mark.asyncio
async def test_npm_version_unavailable_skips_probe_without_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _npm_error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "upstream unavailable"})

    assert (
        await watch_svc.fetch_npm_codex_latest_version(
            transport=httpx.MockTransport(_npm_error_handler)
        )
        is None
    )

    def _npm_connect_fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    assert (
        await watch_svc.fetch_npm_codex_latest_version(
            transport=httpx.MockTransport(_npm_connect_fail)
        )
        is None
    )

    LLM._model_config = None
    mc = ModelConfig()
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-holder-1")

    async def _fake_available(user_id: str, provider: str, *, refresh: bool = False, q: str | None = None):
        if provider == "codex-oauth":
            return {
                "provider": "codex-oauth",
                "status": "ok",
                "fetched_at": "2026-11-18T01:15:00Z",
                "ready": ["gpt-6.2-astra-oauth", "gpt-6.2-sol-oauth"],
                "missing": [],
                "context_windows": {},
                "others": [],
                "total": 2,
                "cached": False,
            }
        raise pm_service.NotConnected(provider)

    monkeypatch.setattr(
        pm_service,
        "available_models",
        create_autospec(pm_service.available_models, side_effect=_fake_available),
    )
    monkeypatch.setattr(
        pm_service,
        "_resolve_credential",
        create_autospec(
            pm_service._resolve_credential,
            side_effect=pm_service.NotConnected("antigravity-oauth"),
        ),
    )
    monkeypatch.setattr(LLM, "get_model_config", create_autospec(LLM.get_model_config, return_value=mc))

    prober_spy = create_autospec(watch_svc.probe_codex_with_client_version)
    report = await watch_svc.run_model_availability_check(
        npm_version_fetcher=create_autospec(watch_svc.fetch_npm_codex_latest_version, return_value=None),
        codex_version_prober=prober_spy,
    )
    assert report["ok"] is True
    assert report["missing_by_provider"] == {}
    assert report["codex_version_probe"]["status"] == "skipped"
    assert report["codex_version_probe"]["reason"] == "npm_version_unavailable"
    prober_spy.assert_not_called()


def test_internal_model_availability_router_requires_service_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "test-service-token-at-least-32-chars-long"
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", token)
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")

    app = FastAPI()
    app.include_router(internal_model_availability_router, prefix="/api/v1/dalton")

    from src.dalton.app import internal_model_availability_router as router_mod

    mock_check = create_autospec(
        watch_svc.run_model_availability_check,
        return_value={"ok": True, "missing_by_provider": {}, "codex_version_probe": {"status": "skipped"}},
    )
    monkeypatch.setattr(router_mod, "run_model_availability_check", mock_check)

    client = TestClient(app)
    unauth = client.post("/api/v1/dalton/internal/model-availability/check")
    assert unauth.status_code == 401
    mock_check.assert_not_called()

    ok_res = client.post(
        "/api/v1/dalton/internal/model-availability/check",
        headers={"X-Service-Token": token},
    )
    assert ok_res.status_code == 200
    assert ok_res.json()["ok"] is True
    mock_check.assert_awaited_once()


@pytest.mark.asyncio
async def test_fallback_or_error_on_supported_provider_reports_error_and_alerts_24h(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    LLM._model_config = None
    mc = ModelConfig()
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-holder-1")
    monkeypatch.setenv("DALTON_CODEX_CLIENT_VERSION", "0.164.0")

    async def _fake_available(
        user_id: str,
        provider: str,
        *,
        refresh: bool = False,
        q: str | None = None,
    ) -> dict[str, Any]:
        if provider == "codex-oauth":
            return {
                "provider": "codex-oauth",
                "status": "fallback",
                "reason": "no_match",
                "fetched_at": "2026-11-18T01:15:00Z",
                "ready": [],
                "missing": ["gpt-6.2-sol-oauth"],
                "context_windows": {},
                "others": [],
                "total": 0,
                "cached": False,
            }
        raise RuntimeError("claude upstream timeout")

    monkeypatch.setattr(
        pm_service,
        "available_models",
        create_autospec(pm_service.available_models, side_effect=_fake_available),
    )
    monkeypatch.setattr(
        pm_service,
        "_resolve_credential",
        create_autospec(
            pm_service._resolve_credential,
            side_effect=pm_service.NotConnected("antigravity-oauth"),
        ),
    )
    monkeypatch.setattr(LLM, "get_model_config", create_autospec(LLM.get_model_config, return_value=mc))

    # Main Codex check fell back with no_match, while higher-version probe succeeds and unlocks models.
    prober_spy = create_autospec(
        watch_svc.probe_codex_with_client_version,
        return_value={
            "provider": "codex-oauth",
            "status": "ok",
            "ready": ["gpt-6.2-sol-oauth"],
            "missing": [],
            "context_windows": {},
            "others": [],
            "total": 1,
            "cached": False,
        },
    )
    report = await watch_svc.run_model_availability_check(
        npm_version_fetcher=create_autospec(watch_svc.fetch_npm_codex_latest_version, return_value="0.175.0"),
        codex_version_prober=prober_spy,
    )

    assert report["ok"] is False
    assert report["providers"]["codex-oauth"]["status"] == "error"
    assert report["providers"]["codex-oauth"]["upstream_status"] == "fallback"
    assert report["providers"]["claude-oauth"]["status"] == "error"
    assert report["error_by_provider"]["codex-oauth"] == "fallback:no_match"
    assert "claude upstream timeout" in report["error_by_provider"]["claude-oauth"]
    assert report["providers"]["antigravity-oauth"]["status"] == "not_connected"
    assert report["providers"]["xai-oauth"]["status"] == "unsupported"
    assert report["codex_version_probe"]["status"] == "ok"
    assert report["codex_version_probe"]["reason"] == "main_check_fallback_probe_unlocked"
    assert report["codex_version_probe"]["upgrade_hint"] == "需要提高 DALTON_CODEX_CLIENT_VERSION 到 0.175.0"

    sent: list[str] = []
    sender_spy = create_autospec(
        lambda d: (True, 200, None),
        side_effect=lambda d: (sent.append(d) or (True, 200, None)),
    )
    t0 = 1_763_428_500.0
    s1, rc1 = watch_cli.run_watch(
        state_dir=tmp_path / "state",
        check_report=report,
        alert_sender=sender_spy,
        now_epoch=t0,
    )
    assert rc1 == 1  # a provider that could not be checked fails the run
    assert s1["ok"] is False
    assert s1["alert"]["sent"] is True
    assert "codex-oauth: 检查失败" in sent[0]
    assert "claude-oauth: 检查失败" in sent[0]

    # Same error within 24h is suppressed.
    s2, rc2 = watch_cli.run_watch(
        state_dir=tmp_path / "state",
        check_report=report,
        alert_sender=sender_spy,
        now_epoch=t0 + 3600,
    )
    assert rc2 == 1
    assert s2["ok"] is False
    assert s2["alert"]["sent"] is False
    assert s2["alert"]["suppressed_24h"] is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_codex_main_fallback_bad_response_probe_ok_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    LLM._model_config = None
    mc = ModelConfig()
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-holder-1")
    monkeypatch.setenv("DALTON_CODEX_CLIENT_VERSION", "0.164.0")

    async def _fake_available(user_id: str, provider: str, *, refresh: bool = False, q: str | None = None):
        if provider == "codex-oauth":
            return {
                "provider": "codex-oauth",
                "status": "fallback",
                "reason": "bad_response",
                "fetched_at": "2026-11-18T01:15:00Z",
                "ready": [],
                "missing": [],
                "context_windows": {},
                "others": [],
                "total": 0,
                "cached": False,
            }
        raise pm_service.NotConnected(provider)

    monkeypatch.setattr(
        pm_service,
        "available_models",
        create_autospec(pm_service.available_models, side_effect=_fake_available),
    )
    monkeypatch.setattr(
        pm_service,
        "_resolve_credential",
        create_autospec(
            pm_service._resolve_credential,
            side_effect=pm_service.NotConnected("antigravity-oauth"),
        ),
    )
    monkeypatch.setattr(LLM, "get_model_config", create_autospec(LLM.get_model_config, return_value=mc))

    prober_spy = create_autospec(
        watch_svc.probe_codex_with_client_version,
        return_value={"status": "ok", "ready": ["gpt-6.2-sol-oauth"], "missing": []},
    )
    report = await watch_svc.run_model_availability_check(
        npm_version_fetcher=create_autospec(watch_svc.fetch_npm_codex_latest_version, return_value="0.175.0"),
        codex_version_prober=prober_spy,
    )
    assert report["ok"] is False
    assert report["providers"]["codex-oauth"]["status"] == "error"
    assert report["codex_version_probe"]["status"] == "ok"
    assert report["codex_version_probe"]["reason"] == "main_check_fallback_probe_ok:bad_response"


def test_separate_24h_dedup_when_missing_same_and_npm_fails_later(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    sent: list[str] = []
    sender_spy = create_autospec(
        lambda d: (True, 200, None),
        side_effect=lambda d: (sent.append(d) or (True, 200, None)),
    )
    t0 = 1_763_428_500.0

    # 1st run: missing set + Codex upgrade hint -> both signatures alert & recorded.
    report_with_hint = {
        "ok": True,
        "missing_by_provider": {"codex-oauth": ["gpt-6.2-sol-oauth"]},
        "error_by_provider": {},
        "codex_version_probe": {
            "status": "ok",
            "reason": "unlocked_models",
            "unlocked_models": ["gpt-6.2-sol-oauth"],
            "upgrade_hint": "需要提高 DALTON_CODEX_CLIENT_VERSION 到 0.175.0",
        },
    }
    s1, rc1 = watch_cli.run_watch(
        state_dir=state_dir,
        check_report=report_with_hint,
        alert_sender=sender_spy,
        now_epoch=t0,
    )
    assert rc1 == 0
    assert s1["alert"]["sent"] is True
    assert s1["alert"]["missing_sent"] is True
    assert s1["alert"]["upgrade_sent"] is True
    assert len(sent) == 1

    # 2nd run within 24h: same missing set, npm lookup failed -> NO duplicate alert!
    report_npm_failed = {
        "ok": True,
        "missing_by_provider": {"codex-oauth": ["gpt-6.2-sol-oauth"]},
        "error_by_provider": {},
        "codex_version_probe": {
            "status": "skipped",
            "reason": "npm_version_unavailable",
            "unlocked_models": [],
            "upgrade_hint": None,
        },
    }
    s2, rc2 = watch_cli.run_watch(
        state_dir=state_dir,
        check_report=report_npm_failed,
        alert_sender=sender_spy,
        now_epoch=t0 + 6 * 3600,
    )
    assert rc2 == 0
    assert s2["alert"]["sent"] is False
    assert s2["alert"]["suppressed_24h"] is True
    assert s2["alert"]["missing_suppressed_24h"] is True
    assert len(sent) == 1

    # 3rd run within 24h: same missing set, newer npm hint 0.176.0 -> sends upgrade hint only, not missing set.
    report_newer_hint = {
        "ok": True,
        "missing_by_provider": {"codex-oauth": ["gpt-6.2-sol-oauth"]},
        "error_by_provider": {},
        "codex_version_probe": {
            "status": "ok",
            "reason": "unlocked_models",
            "unlocked_models": ["gpt-6.2-sol-oauth"],
            "upgrade_hint": "需要提高 DALTON_CODEX_CLIENT_VERSION 到 0.176.0",
        },
    }
    s3, rc3 = watch_cli.run_watch(
        state_dir=state_dir,
        check_report=report_newer_hint,
        alert_sender=sender_spy,
        now_epoch=t0 + 8 * 3600,
    )
    assert rc3 == 0
    assert s3["alert"]["sent"] is True
    assert s3["alert"]["missing_sent"] is False
    assert s3["alert"]["missing_suppressed_24h"] is True
    assert s3["alert"]["upgrade_sent"] is True
    assert len(sent) == 2
    assert "0.176.0" in sent[1]
    assert sent[1].startswith("需要提高 DALTON_CODEX_CLIENT_VERSION 到 0.176.0")



def _mark_neon_awaiting_release(monkeypatch: pytest.MonkeyPatch, mc: Any) -> None:
    from src.dalton.llm import overlay as overlay_mod

    real_meta = overlay_mod.load_dalton_meta() or {}
    meta = {k: dict(v) for k, v in real_meta.items()}
    # Before launch only low/medium/high were configured; xhigh came with GA.
    mc.llm_config.pop("gemini-4-neon-xhigh-oauth", None)
    for key, entry in mc.llm_config.items():
        if isinstance(entry, dict) and key.startswith("gemini-4-neon-"):
            entry["visible"] = False
            meta.setdefault(key, {})["awaiting_release"] = True
    monkeypatch.setattr(watch_svc, "load_dalton_meta", lambda: meta)

@pytest.mark.asyncio
async def test_awaiting_release_missing_suppressed_then_released_alerts_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.dalton.llm import native_catalog

    LLM._model_config = None
    mc = ModelConfig()
    # Neon is GA now; recreate the pre-launch state these tests exercise.
    _mark_neon_awaiting_release(monkeypatch, mc)
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-holder-1")

    async def _fake_available(
        user_id: str,
        provider: str,
        *,
        refresh: bool = False,
        q: str | None = None,
    ) -> dict[str, Any]:
        raise pm_service.NotConnected(provider)

    monkeypatch.setattr(
        pm_service,
        "available_models",
        create_autospec(pm_service.available_models, side_effect=_fake_available),
    )
    monkeypatch.setattr(
        pm_service,
        "_resolve_credential",
        create_autospec(
            pm_service._resolve_credential,
            return_value=pm_service.Credential(api_key="ag-tok", holder="antigravity-oauth"),
        ),
    )
    monkeypatch.setattr(LLM, "get_model_config", create_autospec(LLM.get_model_config, return_value=mc))

    # Build a baseline Antigravity upstream list matching all configured non-Neon models
    # so only Neon is missing in Phase 1.
    non_neon_models = [
        native_catalog.UpstreamModel(id=str(entry["model_id"]), display_name=str(entry["model_id"]))
        for key, entry in mc.llm_config.items()
        if isinstance(entry, dict)
        and entry.get("provider") == "antigravity-oauth"
        and entry.get("visible")
        and not key.startswith("gemini-4-neon-")
    ]

    fetch_ag_spy = create_autospec(
        native_catalog.fetch_antigravity_models,
        return_value=list(non_neon_models),
    )
    monkeypatch.setattr(native_catalog, "fetch_antigravity_models", fetch_ag_spy)
    npm_spy = create_autospec(watch_svc.fetch_npm_codex_latest_version, return_value=None)

    # Phase 1: Neon is missing, awaiting_release=True -> no model_unavailable alert, no model_released alert.
    report_missing = await watch_svc.run_model_availability_check(npm_version_fetcher=npm_spy)
    assert report_missing["ok"] is True
    assert report_missing["missing_by_provider"] == {}
    assert report_missing["released_by_provider"] == {}
    ag_report = report_missing["providers"]["antigravity-oauth"]
    assert ag_report["status"] == "ok"
    assert ag_report["missing_visible"] == []
    assert ag_report["missing_awaiting_release"] == [
        "gemini-4-neon-high-oauth",
        "gemini-4-neon-low-oauth",
        "gemini-4-neon-medium-oauth",
    ]

    sent_alerts: list[tuple[str, str]] = []

    def _fake_sender(detail: str, *, code: str = "model_unavailable", **kwargs: Any):
        sent_alerts.append((code, detail))
        return True, 200, None

    sender_spy = create_autospec(_fake_sender, side_effect=_fake_sender)
    state_dir = tmp_path / "state"
    t0 = 1_763_500_000.0

    s1, rc1 = watch_cli.run_watch(
        state_dir=state_dir,
        check_report=report_missing,
        alert_sender=sender_spy,
        now_epoch=t0,
    )
    assert rc1 == 0
    assert s1["alert"]["sent"] is False
    assert s1["released_alert"]["sent"] is False
    assert sent_alerts == []

    # Phase 2: Neon becomes available -> emits one model_released alert.
    released_models = list(non_neon_models) + [
        native_catalog.UpstreamModel(id="gemini-4-neon-low", display_name="Gemini 4 Neon Low"),
        native_catalog.UpstreamModel(id="gemini-4-neon-medium", display_name="Gemini 4 Neon Medium"),
        native_catalog.UpstreamModel(id="gemini-4-neon-high", display_name="Gemini 4 Neon High"),
    ]
    fetch_ag_spy.return_value = released_models
    report_released = await watch_svc.run_model_availability_check(npm_version_fetcher=npm_spy)
    assert report_released["ok"] is True
    assert report_released["missing_by_provider"] == {}
    assert report_released["released_by_provider"] == {
        "antigravity-oauth": [
            "gemini-4-neon-high-oauth",
            "gemini-4-neon-low-oauth",
            "gemini-4-neon-medium-oauth",
        ]
    }

    s2, rc2 = watch_cli.run_watch(
        state_dir=state_dir,
        check_report=report_released,
        alert_sender=sender_spy,
        now_epoch=t0 + 3600,
    )
    assert rc2 == 0
    assert s2["released_alert"]["sent"] is True
    assert len(sent_alerts) == 1
    assert sent_alerts[0][0] == "model_released"
    assert "antigravity-oauth" in sent_alerts[0][1]
    assert "gemini-4-neon-low-oauth" in sent_alerts[0][1]
    assert "gemini-4-neon-medium-oauth" in sent_alerts[0][1]
    assert "gemini-4-neon-high-oauth" in sent_alerts[0][1]

    # Phase 3: Subsequent run (even >10 days later) does NOT alert again.
    s3, rc3 = watch_cli.run_watch(
        state_dir=state_dir,
        check_report=report_released,
        alert_sender=sender_spy,
        now_epoch=t0 + 10 * 24 * 3600,
    )
    assert rc3 == 0
    assert s3["released_alert"]["sent"] is False
    assert s3["released_alert"]["suppressed_already_alerted"] is True
    assert len(sent_alerts) == 1


@pytest.mark.asyncio
async def test_awaiting_release_unconfigured_prefix_id_triggers_model_released(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.dalton.llm import native_catalog

    LLM._model_config = None
    mc = ModelConfig()
    # Neon is GA now; recreate the pre-launch state these tests exercise.
    _mark_neon_awaiting_release(monkeypatch, mc)
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-holder-1")

    async def _fake_available(
        user_id: str,
        provider: str,
        *,
        refresh: bool = False,
        q: str | None = None,
    ) -> dict[str, Any]:
        raise pm_service.NotConnected(provider)

    monkeypatch.setattr(
        pm_service,
        "available_models",
        create_autospec(pm_service.available_models, side_effect=_fake_available),
    )
    monkeypatch.setattr(
        pm_service,
        "_resolve_credential",
        create_autospec(
            pm_service._resolve_credential,
            return_value=pm_service.Credential(api_key="ag-tok", holder="antigravity-oauth"),
        ),
    )
    monkeypatch.setattr(LLM, "get_model_config", create_autospec(LLM.get_model_config, return_value=mc))

    non_neon_models = [
        native_catalog.UpstreamModel(id=str(entry["model_id"]), display_name=str(entry["model_id"]))
        for key, entry in mc.llm_config.items()
        if isinstance(entry, dict)
        and entry.get("provider") == "antigravity-oauth"
        and entry.get("visible")
        and not key.startswith("gemini-4-neon-")
    ]
    # Subscription exposes gemini-4-neon-xhigh (not configured in overlay).
    upstream_with_xhigh = list(non_neon_models) + [
        native_catalog.UpstreamModel(id="gemini-4-neon-xhigh", display_name="Gemini 4 Neon XHigh"),
        # Internal entry without displayName must not pollute unconfigured release alerts.
        native_catalog.UpstreamModel(id="gemini-4-neon-tiered", display_name=None, internal=True),
    ]
    monkeypatch.setattr(
        native_catalog,
        "fetch_antigravity_models",
        create_autospec(native_catalog.fetch_antigravity_models, return_value=upstream_with_xhigh),
    )

    report = await watch_svc.run_model_availability_check(
        npm_version_fetcher=create_autospec(watch_svc.fetch_npm_codex_latest_version, return_value=None),
    )
    assert report["ok"] is True
    assert report["missing_by_provider"] == {}
    assert report["released_by_provider"] == {"antigravity-oauth": ["gemini-4-neon-xhigh"]}
    assert report["providers"]["antigravity-oauth"]["released_unconfigured"] == ["gemini-4-neon-xhigh"]

    sent_alerts: list[tuple[str, str]] = []

    def _fake_sender(detail: str, *, code: str = "model_unavailable", **kwargs: Any):
        sent_alerts.append((code, detail))
        return True, 200, None

    sender_spy = create_autospec(_fake_sender, side_effect=_fake_sender)
    state_dir = tmp_path / "state"

    s1, rc1 = watch_cli.run_watch(
        state_dir=state_dir,
        check_report=report,
        alert_sender=sender_spy,
        now_epoch=1_763_500_000.0,
    )
    assert rc1 == 0
    assert s1["released_alert"]["sent"] is True
    assert sent_alerts == [
        (
            "model_released",
            "订阅已开放 antigravity-oauth 的 gemini-4-neon-xhigh，核对档位后将其设为可见并去掉 awaiting_release 标记",
        )
    ]

    # Second run does not repeat.
    s2, rc2 = watch_cli.run_watch(
        state_dir=state_dir,
        check_report=report,
        alert_sender=sender_spy,
        now_epoch=1_763_503_600.0,
    )
    assert rc2 == 0
    assert s2["released_alert"]["sent"] is False
    assert len(sent_alerts) == 1

    # Generic prefix derivation check.
    assert watch_svc._awaiting_release_prefix("gemini-4-neon-low") == "gemini-4-neon"
    assert watch_svc._awaiting_release_prefix("claude-opus-6-0-high") == "claude-opus-6-0"
    assert watch_svc._awaiting_release_prefix("gpt-6.3") == "gpt-6.3"


@pytest.mark.asyncio
async def test_hidden_awaiting_model_without_display_name_still_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.llm import native_catalog

    LLM._model_config = None
    mc = ModelConfig()
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-holder-1")
    monkeypatch.setattr(
        pm_service,
        "_resolve_credential",
        create_autospec(
            pm_service._resolve_credential,
            return_value=pm_service.Credential(api_key="ag-tok", holder="antigravity-oauth"),
        ),
    )
    upstream = [
        native_catalog.UpstreamModel(id=str(entry["model_id"]), display_name=str(entry["model_id"]))
        for key, entry in mc.llm_config.items()
        if isinstance(entry, dict)
        and entry.get("provider") == "antigravity-oauth"
        and entry.get("visible")
    ]
    upstream.append(native_catalog.UpstreamModel(id="gemini-4-neon-low", display_name=None, internal=True))
    monkeypatch.setattr(
        native_catalog,
        "fetch_antigravity_models",
        create_autospec(native_catalog.fetch_antigravity_models, return_value=upstream),
    )
    result = await watch_svc._fetch_antigravity_available_models("pm-holder-1", mc)
    other_ids = {item["id"] for item in (result.get("others") or [])}
    ready = set(result.get("ready") or [])
    assert "gemini-4-neon-low" in other_ids or "gemini-4-neon-low-oauth" in ready


def test_native_diff_does_not_mark_awaiting_release_as_deprecated() -> None:
    from src.dalton.llm import native_catalog

    llm_config = {
        "gemini-4-neon-low-oauth": {"provider": "antigravity-oauth", "model_id": "gemini-4-neon-low"},
        "gemini-old-oauth": {"provider": "antigravity-oauth", "model_id": "gemini-old"},
    }
    listing = native_catalog.ProviderListing(
        provider="antigravity-oauth",
        display_name="Antigravity",
        status="ok",
        models=[native_catalog.UpstreamModel(id="gemini-4-hydrogen-low", display_name="H")],
    )
    listings = {p: listing for p in native_catalog.NATIVE_LISTING_PROVIDERS if p == "antigravity-oauth"}
    diff = native_catalog.build_native_diff(
        listings, llm_config, set(), awaiting_release_keys={"gemini-4-neon-low-oauth"}
    )
    keys = {d["key"] for d in diff["deprecated"]}
    assert "gemini-4-neon-low-oauth" not in keys
    assert "gemini-old-oauth" in keys
