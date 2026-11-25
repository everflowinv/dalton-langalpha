"""Unit tests for Research Data Bridge Split Batch 2 (`dalton-transcription`).

Covers docs/dalton/features/researchdata-connectors.md §6.3 and Batch 2 review requirements:
1. Exclusive `<DALTON_ASR_JOBS_DIR>/.owner.lock` mutual exclusion, timeout, and
   holder PID diagnostic format — acquired BEFORE `AsrJobManager` construction
   and `recover_incomplete_jobs()` in both legacy bridge and standalone service.
2. Disable flags (`DALTON_BRIDGE_ASR_DISABLED=true`, `DALTON_TRANSCRIPTION_DISABLED=true`)
   skip lock acquisition and manager construction and immediately return HTTP 503.
3. Drain controls (`POST/GET /transcriptions/drain`, `begin_drain` / `SIGUSR1`
   handler) block new submit/retry with HTTP 503 while allowing in-flight status,
   result, raw download, and cancel until `active_jobs == 0`.
4. Strict authentication isolation (`DALTON_TRANSCRIPTION_TOKEN` fail-closed,
   rejecting bridge tokens) and secret scrubbing in `managed_secret_token_values()`.
5. Backend client migration (`src/dalton/services/transcriptions.py` ->
   `DALTON_TRANSCRIPTION_SERVICE_URL` + `DALTON_TRANSCRIPTION_TOKEN`) and
   capability decoupling (`CapabilityId.TRANSCRIPTION` independent of
   `RESEARCHDATA_BRIDGE`, preserving storage gate).
6. End-to-end cutover & rollback simulation over a shared temp jobs directory:
   historical query, result, `/result/raw` stream, interrupted job recovery,
   retry, cancel, and deduplicated `asr_usage.jsonl` metering.
7. OpenAI-compatible Whisper worker, private Seed-ASR worker registry/factory
   extension hook, and `SourceObjectStore.resolve_full_key` parity.
8. Deployment invariants: `Dockerfile.dalton` (`ffmpeg`), `docker-compose.core.yml`
   (`transcription` profile, `tini` entrypoint bypassing `/docker_entrypoint.sh`,
   `core_internal` + `sandbox-net`, no secret interpolation), Everflow launchd
   template paths, `minio_init.py` optional ASR bucket init, and stdlib-only
   `owner_lock` import without FastAPI/boto3.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import boto3
import httpx
import pytest
import yaml
from fastapi import HTTPException

from src.dalton import capabilities as caps_mod
from src.dalton import settings as dalton_settings
from src.dalton import transcription_storage as tstore
from src.dalton.capabilities import CapabilityId, effective_capabilities
from src.dalton.profile import DeploymentProfile
from src.dalton.services import transcriptions as svc_transcriptions
from src.dalton.transcription_service import app as ts_app
from src.dalton.transcription_service import jobs as ts_jobs
from src.dalton.transcription_service import owner_lock as ts_lock
from src.dalton.transcription_service import whisper_worker as ts_whisper
from src.dalton.transcription_service import worker_base as ts_base
from src.dalton.transcription_service import worker_registry as ts_registry

REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_DIR = REPO_ROOT / "scripts" / "dalton"
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import asr_jobs as legacy_asr_jobs  # noqa: E402
import researchdata_bridge as rb  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bridge(
    tmp_path: Path,
    *,
    jobs_dir: Path | None = None,
    delay_s: float = 0.01,
    fixed_text: str | None = None,
    fail_category: str | None = None,
    asr_disabled: bool = False,
    lock_timeout_s: float = 0.15,
) -> rb.ResearchDataBridge:
    ocw_root = tmp_path / "ocw"
    ocw_root.mkdir(parents=True, exist_ok=True)
    env_file = tmp_path / "bridge.env"
    env_file.write_text("GEMINI_API_KEY=test_key\n", encoding="utf-8")

    cfg = rb.BridgeConfig()
    cfg.env_path = env_file
    cfg.token = "mcp-token-bridge"
    cfg.admin_token = "admin-token-bridge"
    cfg.research_code = ocw_root
    cfg.research_home = ocw_root
    cfg.wiki_ingest_audit_file = tmp_path / "var" / "dalton" / "wiki_ingest_audit.jsonl"
    cfg.wiki_ingest_temp_dir = tmp_path / "var" / "dalton" / "tmp" / "wiki_ingest"
    cfg.asr_jobs_dir = jobs_dir or (tmp_path / "var" / "dalton" / "asr_jobs")
    cfg.asr_fake_delay_s = delay_s
    cfg.asr_fake_text = fixed_text
    cfg.asr_fake_fail_category = fail_category
    cfg.asr_disabled = asr_disabled
    cfg.asr_lock_timeout_s = lock_timeout_s
    return rb.ResearchDataBridge(cfg)


async def _send_bridge_http(
    bridge: rb.ResearchDataBridge,
    method: str,
    path: str,
    headers: dict[str, str],
    body_obj: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    body_bytes = (
        json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
        if body_obj is not None
        else b""
    )
    lines = [f"{method} {path} HTTP/1.1"]
    for k, v in headers.items():
        lines.append(f"{k}: {v}")
    if body_bytes:
        lines.append(f"Content-Length: {len(body_bytes)}")
    raw_req = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body_bytes

    reader = asyncio.StreamReader()
    reader.feed_data(raw_req)
    reader.feed_eof()

    written = bytearray()
    writer = create_autospec(asyncio.StreamWriter, instance=True)
    writer.write.side_effect = lambda data: written.extend(data)
    writer.drain.return_value = None
    writer.wait_closed.return_value = None

    await rb._handle_client(bridge, reader, writer)

    resp_raw = bytes(written).decode("utf-8", errors="replace")
    head, _, body_text = resp_raw.partition("\r\n\r\n")
    status_code = int(head.splitlines()[0].split(" ")[1])
    parsed = json.loads(body_text) if body_text.strip() else {}
    return status_code, parsed


def _sample_submit_payload(
    job_id: str,
    *,
    user_id: str = "u-internal-1",
    workspace_id: str = "ws-cutover",
    thread_id: str = "th-cutover",
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "caller": {
            "user_id": user_id,
            "workspace_id": workspace_id,
            "thread_id": thread_id,
        },
        "source_object_key": f"transcriptions/{workspace_id}/{job_id}/source/call.m4a",
        "source_size_bytes": 240000,
        "source_sha256": "a" * 64,
        "language": "zh",
        "hotwords": ["毛利率", "指南针"],
        "idempotency_key": f"idemp-{job_id}",
    }


# ---------------------------------------------------------------------------
# 1. Exclusive `.owner.lock` mutual exclusion, PID diagnostics & lock-before-construct
# ---------------------------------------------------------------------------


def test_owner_lock_mutual_exclusion_and_pid_diagnostic_format(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "asr_jobs"
    lock1 = ts_lock.AsrOwnerLock(jobs_dir, service_name="researchdata-bridge")
    meta = lock1.acquire(timeout_s=0.1)
    assert lock1.is_held is True
    assert meta.pid == os.getpid()
    assert meta.service == "researchdata-bridge"
    assert (jobs_dir / ts_lock.OWNER_LOCK_FILENAME).is_file()

    # Contending owner fails fast after timeout with canonical PID diagnostics
    lock2 = ts_lock.AsrOwnerLock(jobs_dir, service_name="dalton-transcription", poll_interval_s=0.01)
    with pytest.raises(ts_lock.OwnerLockBusyError) as exc_info:
        lock2.acquire(timeout_s=0.05)

    err = exc_info.value
    msg = str(err)
    assert f"holder_pid={os.getpid()}" in msg
    assert "holder_service=researchdata-bridge" in msg
    assert "holder_started_at=" in msg
    assert "timeout_s=0.05" in msg
    assert str(jobs_dir / ".owner.lock") in msg
    assert lock2.is_held is False

    # Releasing lock1 allows lock2 to acquire immediately
    lock1.release()
    assert lock1.is_held is False
    meta2 = lock2.acquire(timeout_s=0.1)
    assert lock2.is_held is True
    assert meta2.service == "dalton-transcription"
    lock2.release()


@pytest.mark.asyncio
async def test_owner_lock_blocks_before_asr_job_manager_construct_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `.owner.lock` is held, neither new service nor legacy bridge constructs AsrJobManager or calls recover_incomplete_jobs()."""
    jobs_dir = tmp_path / "asr_jobs"

    # Case A: Legacy bridge holds `.owner.lock` -> standalone service fails BEFORE constructing AsrJobManager
    bridge = _make_bridge(tmp_path, jobs_dir=jobs_dir, lock_timeout_s=0.05)
    _ = bridge._asr_job_manager()  # acquires .owner.lock
    assert bridge._asr_owner_lock is not None and bridge._asr_owner_lock.is_held

    mgr_spy = create_autospec(ts_jobs.AsrJobManager)
    monkeypatch.setattr(ts_app, "AsrJobManager", mgr_spy)

    cfg = ts_app.TranscriptionServiceConfig(
        jobs_dir=jobs_dir,
        token="ts-secret-token",
        worker_mode="fake",
        lock_timeout_s=0.05,
    )
    runtime = ts_app.TranscriptionRuntime(cfg, worker=ts_jobs.FakeAsrWorker())
    with pytest.raises(ts_lock.OwnerLockBusyError) as exc_a:
        runtime.startup()
    assert f"holder_pid={os.getpid()}" in str(exc_a.value)
    assert "holder_service=researchdata-bridge" in str(exc_a.value)
    mgr_spy.assert_not_called()
    assert runtime.manager is None
    await bridge.close()

    # Case B: Standalone service holds `.owner.lock` -> legacy bridge fails BEFORE constructing AsrJobManager
    monkeypatch.undo()
    runtime2 = ts_app.TranscriptionRuntime(cfg, worker=ts_jobs.FakeAsrWorker())
    runtime2.startup()
    assert runtime2.owner_lock is not None and runtime2.owner_lock.is_held

    legacy_mgr_spy = create_autospec(legacy_asr_jobs.AsrJobManager)
    monkeypatch.setattr(legacy_asr_jobs, "AsrJobManager", legacy_mgr_spy)

    bridge2 = _make_bridge(tmp_path, jobs_dir=jobs_dir, lock_timeout_s=0.05)
    with pytest.raises(ts_lock.OwnerLockBusyError) as exc_b:
        bridge2._asr_job_manager()
    assert "holder_service=dalton-transcription" in str(exc_b.value)
    legacy_mgr_spy.assert_not_called()

    # Bridge HTTP endpoint surfaces 503 with lock busy diagnostic instead of constructing manager
    code_503, resp_503 = await _send_bridge_http(
        bridge2,
        "GET",
        "/transcriptions/job_any",
        {"Authorization": f"Bearer {bridge2.cfg.admin_token}"},
    )
    assert code_503 == 503
    assert resp_503["error_code"] == "asr_owner_lock_busy"
    assert "holder_service=dalton-transcription" in resp_503["error"]
    legacy_mgr_spy.assert_not_called()

    await bridge2.close()
    await runtime2.shutdown()


# ---------------------------------------------------------------------------
# 2. Disable flags skip lock & manager construction and return 503
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_asr_disabled_skips_lock_and_manager_and_returns_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs_dir = tmp_path / "asr_jobs"
    monkeypatch.setenv("DALTON_BRIDGE_ASR_DISABLED", "true")
    legacy_mgr_spy = create_autospec(legacy_asr_jobs.AsrJobManager)
    monkeypatch.setattr(legacy_asr_jobs, "AsrJobManager", legacy_mgr_spy)

    bridge = _make_bridge(tmp_path, jobs_dir=jobs_dir, asr_disabled=True)
    with pytest.raises(rb.BridgeAsrDisabledError):
        bridge._asr_job_manager()
    legacy_mgr_spy.assert_not_called()
    assert not (jobs_dir / ".owner.lock").exists()

    admin_hdr = {"Authorization": f"Bearer {bridge.cfg.admin_token}"}
    for method, route, body in (
        ("POST", "/transcriptions", _sample_submit_payload("job_dis_1")),
        ("GET", "/transcriptions/job_dis_1", None),
        ("GET", "/transcriptions/job_dis_1/result", None),
        ("GET", "/transcriptions/job_dis_1/result/raw", None),
        ("POST", "/transcriptions/job_dis_1/retry", {}),
        ("POST", "/transcriptions/job_dis_1/cancel", {}),
    ):
        code, resp = await _send_bridge_http(bridge, method, route, admin_hdr, body)
        assert code == 503, f"Expected 503 for {method} {route}, got {code}"
        assert resp["error_code"] == "asr_disabled"

    # Drain status endpoint remains readable for ops scripts
    code_dr, resp_dr = await _send_bridge_http(bridge, "GET", "/transcriptions/drain", admin_hdr)
    assert code_dr == 200
    assert resp_dr["disabled"] is True
    assert resp_dr["owner_lock_held"] is False
    assert resp_dr["ready_for_handoff"] is True
    legacy_mgr_spy.assert_not_called()
    assert not (jobs_dir / ".owner.lock").exists()
    await bridge.close()


@pytest.mark.asyncio
async def test_standalone_service_disabled_skips_lock_and_manager_and_returns_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs_dir = tmp_path / "asr_jobs"
    mgr_spy = create_autospec(ts_jobs.AsrJobManager)
    monkeypatch.setattr(ts_app, "AsrJobManager", mgr_spy)

    cfg = ts_app.TranscriptionServiceConfig(
        jobs_dir=jobs_dir,
        token="ts-disabled-token",
        disabled=True,
    )
    app = ts_app.create_app(config=cfg)
    app.state.runtime.startup()
    try:
        mgr_spy.assert_not_called()
        assert not (jobs_dir / ".owner.lock").exists()

        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": "Bearer ts-disabled-token"}
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            hz = await client.get("/healthz")
            assert hz.status_code == 200
            assert hz.json()["status"] == "disabled"
            assert hz.json()["owner_lock_held"] is False

            for method, route, body in (
                ("POST", "/transcriptions", _sample_submit_payload("job_sd_1")),
                ("GET", "/transcriptions/job_sd_1", None),
                ("GET", "/transcriptions/job_sd_1/result", None),
                ("GET", "/transcriptions/job_sd_1/result/raw", None),
                ("POST", "/transcriptions/job_sd_1/retry", {}),
                ("POST", "/transcriptions/job_sd_1/cancel", {}),
            ):
                resp = await client.request(method, route, headers=headers, json=body)
                assert resp.status_code == 503, f"{method} {route} -> {resp.status_code}"
                assert resp.json()["error_code"] == "service_disabled"
    finally:
        await app.state.runtime.shutdown()


# ---------------------------------------------------------------------------
# 3. Drain controls on standalone service and legacy bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standalone_service_drain_blocks_new_intake_and_allows_inflight_completion(
    tmp_path: Path,
) -> None:
    jobs_dir = tmp_path / "asr_jobs"
    gate = asyncio.Event()
    started = asyncio.Event()

    class _ControlledWorker:
        worker_name = "controlled"

        async def cleanup_job_resources(
            self, job_id: str, segments: list[dict[str, Any]] | None = None
        ) -> None:
            return None

        async def run_job(
            self,
            job_state: dict[str, Any],
            *,
            on_progress: Any,
        ) -> dict[str, Any]:
            await on_progress({"status": "transcribing", "stage": "transcribing"})
            started.set()
            await gate.wait()
            return await ts_jobs.FakeAsrWorker(
                ts_jobs.FakeAsrWorkerConfig(delay_s=0.0, fixed_text="排水测试完成文本。")
            ).run_job(job_state, on_progress=on_progress)

    cfg = ts_app.TranscriptionServiceConfig(
        jobs_dir=jobs_dir,
        token="ts-drain-token",
        lock_timeout_s=0.1,
    )
    app = ts_app.create_app(config=cfg, worker=_ControlledWorker())
    app.state.runtime.startup()
    try:
        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": "Bearer ts-drain-token"}
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            sub = await client.post(
                "/transcriptions",
                headers=headers,
                json=_sample_submit_payload("job_drain_1"),
            )
            assert sub.status_code == 200
            await asyncio.wait_for(started.wait(), timeout=2.0)

            # Enter drain mode via HTTP control endpoint
            dr = await client.post("/transcriptions/drain", headers=headers, json={"drain": True})
            assert dr.status_code == 200
            dr_body = dr.json()
            assert dr_body["draining"] is True
            assert dr_body["active_jobs"] == 1
            assert dr_body["active_job_ids"] == ["job_drain_1"]
            assert dr_body["ready_for_handoff"] is False

            # New submit and retry are rejected with 503 while draining
            sub2 = await client.post(
                "/transcriptions",
                headers=headers,
                json=_sample_submit_payload("job_drain_new"),
            )
            assert sub2.status_code == 503
            assert sub2.json()["error_code"] == "service_draining"

            ret = await client.post(
                "/transcriptions/job_drain_1/retry", headers=headers, json={}
            )
            assert ret.status_code == 503
            assert ret.json()["error_code"] == "service_draining"

            # Status query continues working while draining
            st_mid = await client.get("/transcriptions/job_drain_1", headers=headers)
            assert st_mid.status_code == 200
            assert st_mid.json()["job"]["status"] == "transcribing"

            # Let in-flight job finish
            gate.set()
            for _ in range(50):
                st_end = await client.get("/transcriptions/job_drain_1", headers=headers)
                if st_end.json()["job"]["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("In-flight job did not finish during drain")

            # Drain status and /healthz now report ready_for_handoff=True
            dr_after = await client.get("/transcriptions/drain", headers=headers)
            assert dr_after.status_code == 200
            assert dr_after.json()["active_jobs"] == 0
            assert dr_after.json()["ready_for_handoff"] is True

            hz = await client.get("/healthz")
            assert hz.status_code == 200
            assert hz.json()["status"] == "draining"
            assert hz.json()["ready_for_handoff"] is True

            # Result and raw download still work during drain
            res = await client.get("/transcriptions/job_drain_1/result", headers=headers)
            assert res.status_code == 200
            assert res.json()["result"]["text"] == "排水测试完成文本。"
            raw = await client.get("/transcriptions/job_drain_1/result/raw", headers=headers)
            assert raw.status_code == 200
            assert raw.json()["job_id"] == "job_drain_1"

            # Resuming intake re-opens submissions
            resumed = await client.post(
                "/transcriptions/drain", headers=headers, json={"drain": False}
            )
            assert resumed.status_code == 200
            assert resumed.json()["draining"] is False
    finally:
        await app.state.runtime.shutdown()


@pytest.mark.asyncio
async def test_bridge_drain_blocks_new_intake_and_reports_ready_for_handoff(
    tmp_path: Path,
) -> None:
    jobs_dir = tmp_path / "asr_jobs"
    bridge = _make_bridge(tmp_path, jobs_dir=jobs_dir, delay_s=0.01)
    admin_hdr = {"Authorization": f"Bearer {bridge.cfg.admin_token}"}
    try:
        code_dr, resp_dr = await _send_bridge_http(
            bridge, "POST", "/transcriptions/drain", admin_hdr, {"drain": True}
        )
        assert code_dr == 200
        assert resp_dr["draining"] is True
        assert resp_dr["ready_for_handoff"] is True

        code_sub, resp_sub = await _send_bridge_http(
            bridge, "POST", "/transcriptions", admin_hdr, _sample_submit_payload("job_bdr_1")
        )
        assert code_sub == 503
        assert resp_sub["error_code"] == "asr_draining"

        code_ret, resp_ret = await _send_bridge_http(
            bridge, "POST", "/transcriptions/job_bdr_1/retry", admin_hdr, {}
        )
        assert code_ret == 503
        assert resp_ret["error_code"] == "asr_draining"

        code_hz, resp_hz = await _send_bridge_http(bridge, "GET", "/healthz", {})
        assert code_hz == 200
        assert resp_hz["asr"]["draining"] is True
    finally:
        await bridge.close()


# ---------------------------------------------------------------------------
# 4. Authentication isolation & secret scrubbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standalone_service_auth_fail_closed_and_rejects_bridge_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs_dir = tmp_path / "asr_jobs"
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "bridge-admin-only")
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", "bridge-mcp-only")
    monkeypatch.delenv("DALTON_TRANSCRIPTION_TOKEN", raising=False)

    # Fail closed when DALTON_TRANSCRIPTION_TOKEN is unset
    cfg_empty = ts_app.TranscriptionServiceConfig(jobs_dir=jobs_dir, token="", lock_timeout_s=0.1)
    app_empty = ts_app.create_app(config=cfg_empty, worker=ts_jobs.FakeAsrWorker())
    app_empty.state.runtime.startup()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_empty), base_url="http://test"
        ) as client:
            r_unauth = await client.get(
                "/transcriptions/drain",
                headers={"Authorization": "Bearer bridge-admin-only"},
            )
            assert r_unauth.status_code == 401
    finally:
        await app_empty.state.runtime.shutdown()

    # Configured independent token rejects missing/wrong/bridge tokens and accepts Bearer & X-Dalton-Transcription-Token
    cfg = ts_app.TranscriptionServiceConfig(
        jobs_dir=jobs_dir, token="independent-ts-token-32bytes", lock_timeout_s=0.1
    )
    app = ts_app.create_app(config=cfg, worker=ts_jobs.FakeAsrWorker())
    app.state.runtime.startup()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for bad_headers in (
                {},
                {"Authorization": "Bearer wrong-token"},
                {"Authorization": "Bearer bridge-admin-only"},
                {"Authorization": "Bearer bridge-mcp-only"},
                {"X-Dalton-Bridge-Token": "bridge-admin-only"},
            ):
                r = await client.get("/transcriptions/drain", headers=bad_headers)
                assert r.status_code == 401

            r_bearer = await client.get(
                "/transcriptions/drain",
                headers={"Authorization": "Bearer independent-ts-token-32bytes"},
            )
            assert r_bearer.status_code == 200

            r_hdr = await client.get(
                "/transcriptions/drain",
                headers={"X-Dalton-Transcription-Token": "independent-ts-token-32bytes"},
            )
            assert r_hdr.status_code == 200

            hz = await client.get("/healthz")
            assert hz.status_code == 200
            assert "independent-ts-token-32bytes" not in hz.text
    finally:
        await app.state.runtime.shutdown()


# ---------------------------------------------------------------------------
# 5. Capability decoupling, profile URL defaults & backend client switch
# ---------------------------------------------------------------------------


def test_transcription_capability_independent_of_researchdata_bridge_but_requires_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "core")
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.setenv("DALTON_ENABLED", "true")
    monkeypatch.delenv("DALTON_CAP_RESEARCHDATA_BRIDGE", raising=False)
    monkeypatch.delenv("DALTON_RESEARCHDATA_URL", raising=False)
    monkeypatch.delenv("DALTON_BRIDGE_URL", raising=False)
    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")

    assert caps_mod.CAPABILITY_SPECS[CapabilityId.TRANSCRIPTION].depends_on == ()

    # Core with RESEARCHDATA_BRIDGE OFF + storage ON -> TRANSCRIPTION enabled
    caps_on = effective_capabilities(transcription_storage_configured=True)
    assert CapabilityId.RESEARCHDATA_BRIDGE not in caps_on
    assert CapabilityId.TRANSCRIPTION in caps_on

    # Core with storage OFF -> TRANSCRIPTION disabled
    caps_off = effective_capabilities(transcription_storage_configured=False)
    assert CapabilityId.TRANSCRIPTION not in caps_off


def test_backend_settings_resolve_connector_env_defaults_and_secret_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert dalton_settings.resolve_connector_env_defaults(DeploymentProfile.CORE) == {
        "DALTON_TRANSCRIPTION_SERVICE_URL": "http://transcription:8965",
    }
    assert dalton_settings.resolve_connector_env_defaults(DeploymentProfile.EVERFLOW) == {
        "DALTON_TRANSCRIPTION_SERVICE_URL": "http://host.docker.internal:8965",
    }

    monkeypatch.setenv("DALTON_TRANSCRIPTION_TOKEN", "super-secret-ts-token-999")
    assert "super-secret-ts-token-999" in dalton_settings.managed_secret_token_values()
    assert (
        dalton_settings.redact_managed_tokens("Authorization: Bearer super-secret-ts-token-999")
        == "Authorization: Bearer ***"
    )

    # Profile-based URL resolution when transcription is enabled in Core without explicit URL
    monkeypatch.setenv("APP_ENV", "core")
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.setenv("DALTON_ENABLED", "true")
    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")
    monkeypatch.delenv("DALTON_TRANSCRIPTION_SERVICE_URL", raising=False)
    monkeypatch.setattr(
        svc_transcriptions,
        "is_transcription_storage_configured",
        create_autospec(tstore.is_transcription_storage_configured, return_value=True),
    )
    assert dalton_settings.get_dalton_transcription_service_url() == "http://transcription:8965"

    # Explicit override respected
    monkeypatch.setenv("DALTON_TRANSCRIPTION_SERVICE_URL", "http://custom-asr:9965/")
    assert dalton_settings.get_dalton_transcription_service_url() == "http://custom-asr:9965"

    # When transcription capability is off -> empty string
    monkeypatch.setenv("DALTON_CAP_TRANSCRIPTION", "false")
    assert dalton_settings.get_dalton_transcription_service_url() == ""


@pytest.mark.asyncio
async def test_backend_client_calls_standalone_service_with_independent_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backend `src/dalton/services/transcriptions.py` hits `DALTON_TRANSCRIPTION_SERVICE_URL` + `DALTON_TRANSCRIPTION_TOKEN` with `RESEARCHDATA_BRIDGE` OFF."""
    jobs_dir = tmp_path / "asr_jobs"
    ts_token = "standalone-backend-ts-token-0123456789"
    cfg = ts_app.TranscriptionServiceConfig(
        jobs_dir=jobs_dir,
        token=ts_token,
        lock_timeout_s=0.1,
    )
    app = ts_app.create_app(
        config=cfg,
        worker=ts_jobs.FakeAsrWorker(
            ts_jobs.FakeAsrWorkerConfig(delay_s=0.01, fixed_text="独立转写服务返回文本。")
        ),
    )
    app.state.runtime.startup()

    # Core profile, RESEARCHDATA_BRIDGE completely off, DALTON_BRIDGE_ADMIN_TOKEN unset
    monkeypatch.setenv("APP_ENV", "core")
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.setenv("DALTON_ENABLED", "true")
    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")
    monkeypatch.delenv("DALTON_BRIDGE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("DALTON_RESEARCHDATA_URL", raising=False)
    monkeypatch.delenv("DALTON_BRIDGE_URL", raising=False)
    monkeypatch.setenv("DALTON_TRANSCRIPTION_SERVICE_URL", "http://transcription:8965")
    monkeypatch.setattr(
        svc_transcriptions,
        "is_transcription_storage_configured",
        create_autospec(tstore.is_transcription_storage_configured, return_value=True),
    )

    try:
        # Missing DALTON_TRANSCRIPTION_TOKEN -> fails closed with HTTP 503
        monkeypatch.delenv("DALTON_TRANSCRIPTION_TOKEN", raising=False)
        with pytest.raises(HTTPException) as exc_missing:
            await svc_transcriptions.call_bridge_submit_transcription(
                _sample_submit_payload("job_be_fail")
            )
        assert exc_missing.value.status_code == 503
        assert "DALTON_TRANSCRIPTION_TOKEN" in str(exc_missing.value.detail)

        # Configure independent token and route httpx.AsyncClient through ASGITransport
        monkeypatch.setenv("DALTON_TRANSCRIPTION_TOKEN", ts_token)
        seen_urls: list[str] = []
        seen_auth: list[str] = []
        real_async_client = httpx.AsyncClient

        def _asgi_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.ASGITransport(app=app)
            client = real_async_client(*args, **kwargs)
            orig_send = client.send

            async def _recording_send(request: httpx.Request, **send_kwargs: Any) -> httpx.Response:
                seen_urls.append(str(request.url))
                seen_auth.append(request.headers.get("Authorization", ""))
                return await orig_send(request, **send_kwargs)

            client.send = _recording_send  # type: ignore[method-assign]
            return client

        monkeypatch.setattr(svc_transcriptions.httpx, "AsyncClient", _asgi_client)

        code_sub, body_sub = await svc_transcriptions.call_bridge_submit_transcription(
            _sample_submit_payload("job_be_ok")
        )
        assert code_sub == 200
        assert body_sub["ok"] is True

        for _ in range(50):
            code_st, body_st = await svc_transcriptions.call_bridge_get_transcription("job_be_ok")
            assert code_st == 200
            if body_st["job"]["status"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Job submitted by backend client did not reach succeeded")

        code_res, body_res = await svc_transcriptions.call_bridge_get_transcription_result("job_be_ok")
        assert code_res == 200
        assert body_res["result"]["text"] == "独立转写服务返回文本。"

        code_raw, stream_or_err = await svc_transcriptions.open_bridge_raw_result_stream("job_be_ok")
        assert code_raw == 200
        assert not isinstance(stream_or_err, dict)
        chunks = [chunk async for chunk in stream_or_err]
        parsed_raw = json.loads(b"".join(chunks).decode("utf-8"))
        assert parsed_raw["job_id"] == "job_be_ok"

        assert all(u.startswith("http://transcription:8965/transcriptions") for u in seen_urls)
        assert all(a == f"Bearer {ts_token}" for a in seen_auth)
    finally:
        await app.state.runtime.shutdown()


# ---------------------------------------------------------------------------
# 6. Cutover & rollback continuity over shared jobs_dir & asr_usage.jsonl
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cutover_bridge_to_standalone_preserves_history_inflight_download_cancel_retry_and_usage(
    tmp_path: Path,
) -> None:
    """Simulate full §6.3 cutover from legacy bridge to standalone service on the same temp directory."""
    jobs_dir = tmp_path / "var" / "dalton" / "asr_jobs"
    usage_file = jobs_dir / "asr_usage.jsonl"

    # Phase 1: Legacy bridge creates historical succeeded job A, failed job B, interrupted job C, cancel-target job D
    bridge_ok = _make_bridge(
        tmp_path, jobs_dir=jobs_dir, delay_s=0.01, fixed_text="历史任务A的转写结果。"
    )
    code_a, _ = await bridge_ok.handle_transcription_submit(_sample_submit_payload("job_hist_a"))
    assert code_a == 200
    for _ in range(50):
        _, st_a = await bridge_ok.handle_transcription_status("job_hist_a")
        if st_a["job"]["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Historical job A did not succeed on legacy bridge")
    await bridge_ok.close()

    # Failed job B on legacy bridge
    bridge_fail = _make_bridge(
        tmp_path, jobs_dir=jobs_dir, delay_s=0.01, fail_category="asr_upstream_timeout"
    )
    code_b, _ = await bridge_fail.handle_transcription_submit(_sample_submit_payload("job_fail_b"))
    assert code_b == 200
    for _ in range(50):
        _, st_b = await bridge_fail.handle_transcription_status("job_fail_b")
        if st_b["job"]["status"] == "failed":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Failed job B did not fail on legacy bridge")
    await bridge_fail.close()

    # Interrupted in-flight job C + queued job D on legacy bridge, then drain + shutdown
    bridge_slow = _make_bridge(tmp_path, jobs_dir=jobs_dir, delay_s=5.0)
    await bridge_slow.handle_transcription_submit(_sample_submit_payload("job_inflight_c"))
    await bridge_slow.handle_transcription_submit(_sample_submit_payload("job_cancel_d"))
    mgr_slow = bridge_slow._asr_job_manager()
    for _ in range(30):
        sc = mgr_slow.read_state("job_inflight_c")
        sd = mgr_slow.read_state("job_cancel_d")
        if sc and sc.get("status") == "transcribing" and sd and sd.get("status") == "transcribing":
            break
        await asyncio.sleep(0.01)

    # Drain legacy bridge and shut down cleanly -> marks C and D interrupted (not cancelled) and releases .owner.lock
    bridge_slow.set_asr_draining(True)
    await bridge_slow.close()
    assert json.loads((jobs_dir / "job_inflight_c" / "state.json").read_text())["interrupted"] is True
    assert usage_file.is_file()
    pre_cutover_records = ts_jobs.read_usage_jsonl(usage_file)
    assert {(r["job_id"], r["attempt"]) for r in pre_cutover_records} == {
        ("job_hist_a", 1),
        ("job_fail_b", 1),
    }

    # Phase 2: Standalone dalton-transcription starts on the SAME jobs_dir & usage_file
    gate_d = asyncio.Event()

    class _CutoverWorker:
        worker_name = "cutover-test"

        async def cleanup_job_resources(
            self, job_id: str, segments: list[dict[str, Any]] | None = None
        ) -> None:
            return None

        async def run_job(self, job_state: dict[str, Any], *, on_progress: Any) -> dict[str, Any]:
            jid = str(job_state["job_id"])
            if jid == "job_cancel_d":
                await on_progress({"status": "transcribing", "stage": "transcribing"})
                await gate_d.wait()
            return await ts_jobs.FakeAsrWorker(
                ts_jobs.FakeAsrWorkerConfig(
                    delay_s=0.01, fixed_text=f"新服务完成:{jid}-a{job_state.get('attempt', 1)}"
                )
            ).run_job(job_state, on_progress=on_progress)

    cfg = ts_app.TranscriptionServiceConfig(
        jobs_dir=jobs_dir,
        usage_ledger_path=usage_file,
        token="cutover-ts-token",
        lock_timeout_s=0.2,
    )
    app = ts_app.create_app(config=cfg, worker=_CutoverWorker())
    resumed = app.state.runtime.startup()
    assert set(resumed) == {"job_inflight_c", "job_cancel_d"}

    try:
        headers = {"Authorization": "Bearer cutover-ts-token"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # 1) Historical completed job A: query + result + raw stream
            r_a_st = await client.get("/transcriptions/job_hist_a", headers=headers)
            assert r_a_st.status_code == 200
            assert r_a_st.json()["job"]["status"] == "succeeded"

            r_a_res = await client.get("/transcriptions/job_hist_a/result", headers=headers)
            assert r_a_res.status_code == 200
            assert r_a_res.json()["result"]["text"] == "历史任务A的转写结果。"

            r_a_raw = await client.get("/transcriptions/job_hist_a/result/raw", headers=headers)
            assert r_a_raw.status_code == 200
            assert r_a_raw.json()["job_id"] == "job_hist_a"

            # 2) Interrupted job C: automatically recovered to succeeded
            for _ in range(50):
                r_c = await client.get("/transcriptions/job_inflight_c", headers=headers)
                if r_c.json()["job"]["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("Interrupted job C did not finish after cutover")
            r_c_res = await client.get("/transcriptions/job_inflight_c/result", headers=headers)
            assert r_c_res.status_code == 200
            assert r_c_res.json()["result"]["text"] == "新服务完成:job_inflight_c-a1"

            # 3) Failed job B: retry on new service succeeds (attempt 2)
            r_b_ret = await client.post("/transcriptions/job_fail_b/retry", headers=headers, json={})
            assert r_b_ret.status_code == 200
            assert r_b_ret.json()["job"]["attempt"] == 2
            for _ in range(50):
                r_b = await client.get("/transcriptions/job_fail_b", headers=headers)
                if r_b.json()["job"]["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("Retried job B did not succeed on new service")
            r_b_res = await client.get("/transcriptions/job_fail_b/result", headers=headers)
            assert r_b_res.json()["result"]["text"] == "新服务完成:job_fail_b-a2"

            # 4) In-flight job D: cancel on new service succeeds
            r_d_cancel = await client.post("/transcriptions/job_cancel_d/cancel", headers=headers, json={})
            assert r_d_cancel.status_code == 200
            assert r_d_cancel.json()["job"]["status"] == "cancelled"

        # 5) Usage ledger contains deduplicated pre-cutover + post-cutover records
        all_records = ts_jobs.read_usage_jsonl(usage_file)
        keys = {(r["job_id"], r["attempt"], r["status"]) for r in all_records}
        assert ("job_hist_a", 1, "succeeded") in keys
        assert ("job_fail_b", 1, "failed") in keys
        assert ("job_fail_b", 2, "succeeded") in keys
        assert ("job_inflight_c", 1, "succeeded") in keys
        assert ("job_cancel_d", 1, "cancelled") in keys
    finally:
        gate_d.set()
        await app.state.runtime.shutdown()


# ---------------------------------------------------------------------------
# 7. OpenAI-compatible Whisper worker, extension registry & key parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whisper_worker_end_to_end_split_segments_and_cleanup(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "asr_jobs"
    audio_bytes = b"fake-mp3-audio-payload-1234567890"
    audio_sha = hashlib.sha256(audio_bytes).hexdigest()

    def _fake_download(
        source_object_key: str,
        dest_path: Path,
        *,
        expected_size_bytes: int,
        expected_sha256: str,
        chunk_size: int = 1024 * 1024,
        deadline_monotonic: float | None = None,
    ) -> tuple[int, str]:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(audio_bytes)
        return len(audio_bytes), audio_sha

    source_store = create_autospec(ts_base.SourceObjectStore, instance=True)
    source_store.download_to_path.side_effect = _fake_download

    async def _fake_split(
        source_path: Path,
        chunks_dir: Path,
        *,
        total_duration_s: float,
        segment_time_s: int = 600,
        ffmpeg_bin: Any = None,
        timeout_s: float = 600.0,
        transcode_semaphore: Any = None,
        base_env: Any = None,
    ) -> list[ts_base.PreparedSegmentFile]:
        chunks_dir.mkdir(parents=True, exist_ok=True)
        c0 = chunks_dir / "chunk_00.mp3"
        c1 = chunks_dir / "chunk_01.mp3"
        c0.write_bytes(b"chunk0")
        c1.write_bytes(b"chunk1")
        return [
            ts_base.PreparedSegmentFile(
                index=0, path=c0, start_s=0.0, end_s=600.0, sha256="1" * 64, size_bytes=6, codec_params={"format": "mp3"}
            ),
            ts_base.PreparedSegmentFile(
                index=1, path=c1, start_s=600.0, end_s=1900.0, sha256="2" * 64, size_bytes=6, codec_params={"format": "mp3"}
            ),
        ]

    probe_fn = create_autospec(ts_base.probe_audio_duration, return_value=1900.0)
    split_fn = create_autospec(ts_base.split_audio_to_mp3_segments, side_effect=_fake_split)

    call_idx = 0
    seen_auth_headers: list[str] = []

    def _whisper_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_idx
        assert str(request.url) == "https://whisper.example.test/v1/audio/transcriptions"
        seen_auth_headers.append(request.headers.get("Authorization", ""))
        idx = call_idx
        call_idx += 1
        return httpx.Response(
            200,
            headers={"x-request-id": f"req-whisper-{idx}"},
            json={
                "text": f"segment {idx} text",
                "duration": 600.0 if idx == 0 else 1300.0,
                "segments": [
                    {"start": 1.0, "end": 5.5, "text": f"Part {idx} utterance"},
                ],
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_whisper_handler))
    try:
        worker = ts_whisper.OpenAICompatibleWhisperWorker(
            jobs_dir,
            config=ts_whisper.WhisperWorkerConfig(
                api_base_url="https://whisper.example.test/v1",
                api_key="sk-whisper-secret-test",
                model="whisper-1",
            ),
            source_store=source_store,
            http_client=http_client,
            probe_duration_fn=probe_fn,
            split_segments_fn=split_fn,
        )
        progress_stages: list[str] = []

        async def _on_progress(patch: dict[str, Any]) -> bool:
            if "stage" in patch:
                progress_stages.append(str(patch["stage"]))
            return True

        outcome = await worker.run_job(
            {
                "job_id": "job_whisper_01",
                "attempt": 1,
                "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
                "source_object_key": "transcriptions/ws-1/job_whisper_01/source/long.mp3",
                "source_size_bytes": len(audio_bytes),
                "source_sha256": audio_sha,
                "language": "zh-CN",
                "hotwords": ["毛利率"],
            },
            on_progress=_on_progress,
        )

        assert outcome["status"] == "succeeded"
        assert outcome["segment_count"] == 2
        assert outcome["completed_segments"] == 2
        assert outcome["provider_request_ids"] == ["req-whisper-0", "req-whisper-1"]
        assert seen_auth_headers == ["Bearer sk-whisper-secret-test", "Bearer sk-whisper-secret-test"]
        assert "downloading" in progress_stages
        assert "probing" in progress_stages
        assert "splitting" in progress_stages
        assert "assembling" in progress_stages

        utterances = outcome["result"]["utterances"]
        assert len(utterances) == 2
        # Segment 0 offset = 0ms; Segment 1 offset = 600_000ms
        assert utterances[0]["start_ms"] == 1000
        assert utterances[0]["speaker"] == "S1-Speaker 1"
        assert utterances[1]["start_ms"] == 601000
        assert utterances[1]["raw_start_ms"] == 1000
        assert utterances[1]["speaker"] == "S2-Speaker 1"
        assert (jobs_dir / "job_whisper_01" / "raw_response.json").is_file()
        assert (jobs_dir / "job_whisper_01" / "transcript.txt").is_file()
        # Local temporary work directory is cleaned up after terminal state
        assert not (jobs_dir / "job_whisper_01" / "work").exists()
    finally:
        await http_client.aclose()


def test_worker_registry_extension_hook_and_factory_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs_dir = tmp_path / "asr_jobs"

    # 1) No providers configured in auto mode -> UnconfiguredAsrWorker (never fakes silently)
    clean_env = {"DALTON_ASR_WORKER_MODE": "auto"}
    w_none = ts_registry.resolve_worker(jobs_dir, environ=clean_env)
    assert isinstance(w_none, ts_registry.UnconfiguredAsrWorker)

    # 2) Whisper configured in auto mode -> OpenAICompatibleWhisperWorker
    w_whisper = ts_registry.resolve_worker(
        jobs_dir,
        environ={"DALTON_ASR_WORKER_MODE": "auto", "DALTON_WHISPER_API_KEY": "sk-test-whisper"},
    )
    assert isinstance(w_whisper, ts_whisper.OpenAICompatibleWhisperWorker)

    # 3) Private Seed-ASR worker extension registered programmatically
    sentinel_worker = ts_jobs.FakeAsrWorker(ts_jobs.FakeAsrWorkerConfig(fixed_text="private-seed"))
    factory_called: list[Path] = []

    def _private_seed_factory(
        *, jobs_dir: Path, atomic_write_fn: Any = None, environ: Any = None
    ) -> ts_base.AsrWorkerProtocol:
        factory_called.append(jobs_dir)
        return sentinel_worker

    ts_registry.register_worker_factory("seed_asr", _private_seed_factory)
    try:
        assert "seed_asr" in ts_registry.list_registered_worker_factories()
        w_seed = ts_registry.resolve_worker(
            jobs_dir, environ={"DALTON_ASR_WORKER_MODE": "seed_asr"}
        )
        assert w_seed is sentinel_worker
        assert factory_called == [jobs_dir]
    finally:
        ts_registry.unregister_worker_factory("seed_asr")


def test_source_object_store_key_parity_with_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    for prefix in ("", "dalton", "/dalton/asr/"):
        monkeypatch.setenv("DALTON_ASR_STORAGE_KEY_PREFIX", prefix)
        store = ts_base.SourceObjectStore()
        for key in (
            "transcriptions/ws-1/job-1/source/a.m4a",
            "/transcriptions/ws-1/job-1/source/a.m4a",
            "dalton/transcriptions/ws-1/job-1/source/a.m4a",
        ):
            assert store.resolve_full_key(key) == tstore.resolve_full_key(key)


# ---------------------------------------------------------------------------
# 8. Deployment & packaging invariants (Codex r3 #5 + §5.1 + §6.3)
# ---------------------------------------------------------------------------


def test_deployment_configs_ffmpeg_entrypoint_networks_launchd_and_minio_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1) Dockerfile.dalton installs ffmpeg (includes ffprobe)
    dockerfile = (REPO_ROOT / "scripts/dalton/Dockerfile.dalton").read_text(encoding="utf-8")
    assert re.search(r"\bffmpeg\b", dockerfile), "Dockerfile.dalton must install ffmpeg for transcription service"

    # 2) docker-compose.core.yml defines transcription under profiles: ["transcription"],
    #    reuses backend image without build:, bypasses /docker_entrypoint.sh via tini,
    #    joins core_internal + sandbox-net, and never Compose-interpolates secrets.
    class _ComposeLoader(yaml.SafeLoader):
        pass

    def _passthrough(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_scalar(node)

    _ComposeLoader.add_constructor("!reset", _passthrough)
    _ComposeLoader.add_constructor("!override", _passthrough)

    compose_raw = (REPO_ROOT / "docker-compose.core.yml").read_text(encoding="utf-8")
    compose = yaml.load(compose_raw, Loader=_ComposeLoader)
    ts_svc = compose["services"]["transcription"]
    assert ts_svc["profiles"] == ["transcription"]
    assert "build" not in ts_svc
    assert ts_svc["image"] == "${DALTON_BACKEND_IMAGE:-dalton-core-backend:local}"
    assert ts_svc["entrypoint"] == ["/usr/bin/tini", "-s", "--"]
    assert set(ts_svc["networks"]) == {"core_internal", "sandbox-net"}
    assert "dalton-asr-jobs:/var/lib/dalton/asr_jobs" in ts_svc["volumes"]
    assert "dalton-asr-jobs" in compose["volumes"]
    for secret_key in (
        "DALTON_TRANSCRIPTION_TOKEN",
        "DALTON_TRANSCRIPTION_TICKET_SECRET",
        "DALTON_ASR_STORAGE_ACCESS_KEY_ID",
        "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY",
        "DALTON_WHISPER_API_KEY",
        "OPENAI_API_KEY",
    ):
        assert not re.search(rf"(?<!\$)\$\{{{secret_key}\b", compose_raw), (
            f"docker-compose.core.yml must not Compose-interpolate secret {secret_key}"
        )

    # 3) Everflow launchd plist template is valid XML and uses identical default production paths
    plist_path = REPO_ROOT / "scripts/dalton/launchd/com.dalton.transcription.plist.template"
    plist_data = plistlib.loads(plist_path.read_bytes())
    assert plist_data["Label"] == "com.dalton.transcription"
    env_vars = plist_data["EnvironmentVariables"]
    assert env_vars["DALTON_ASR_JOBS_DIR"] == "__REPO__/var/dalton/asr_jobs"
    assert env_vars["DALTON_ASR_USAGE_FILE"] == "__REPO__/var/dalton/asr_jobs/asr_usage.jsonl"
    assert env_vars["DALTON_TRANSCRIPTION_PORT"] == "8965"
    assert env_vars["DALTON_HOST_PLATFORM"] == "macos"

    # 4) Default code path matches legacy researchdata_bridge.py (`<repo>/var/dalton/asr_jobs`)
    assert ts_app.default_asr_jobs_dir() == REPO_ROOT / "var" / "dalton" / "asr_jobs"

    # 5) minio_init.py initializes optional DALTON_ASR_STORAGE_BUCKET_NAME when set
    spec = importlib.util.spec_from_file_location(
        "minio_init_b2_test", REPO_ROOT / "scripts/standalone/minio_init.py"
    )
    assert spec and spec.loader
    minio_init = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(minio_init)

    s3_spec = boto3.client(
        "s3",
        endpoint_url="http://minio:9000",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        region_name="us-east-1",
    )
    fake_s3 = create_autospec(s3_spec)
    monkeypatch.setattr(minio_init.boto3, "client", lambda *a, **kw: fake_s3)
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "chat-uploads")
    monkeypatch.setenv("MINIO_ROOT_USER", "ak")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "sk")
    monkeypatch.setenv("DALTON_ASR_STORAGE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("DALTON_ASR_STORAGE_BUCKET_NAME", "transcriptions")

    assert minio_init.main() == 0
    buckets_checked = [call.kwargs["Bucket"] for call in fake_s3.head_bucket.call_args_list]
    assert "chat-uploads" in buckets_checked
    assert "transcriptions" in buckets_checked

    # 6) owner_lock imports cleanly without FastAPI / uvicorn / boto3 (system Homebrew Python contract)
    probe_code = (
        "import sys\n"
        "for blocked in ('fastapi', 'uvicorn', 'boto3', 'botocore'):\n"
        "    sys.modules[blocked] = None\n"
        "from src.dalton.transcription_service.owner_lock import AsrOwnerLock, OwnerLockBusyError\n"
        "assert AsrOwnerLock is not None and OwnerLockBusyError is not None\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe_code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
