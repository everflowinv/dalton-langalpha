"""Unit & real-subprocess acceptance tests for Batch 3b Steps 1 & 2 (§6.4b (7)).

Covers:
  Step 1:
    - Shared deployment capability env (`load_deployment_capability_env`, `find_capability_env_conflicts`)
      across backend, `dalton-research-host`, and `researchdata_bridge` + `route_migrated` consistency.
    - PostgreSQL transaction advisory lock serialization (`pg_advisory_xact_lock_shared` vs
      `pg_advisory_xact_lock`) at `begin_run` `before_commit`, `subagent_runs`, `dalton_data_task_slot`
      claim, ingest tools, and all HTTP/background admission entries + `/maintenance/admission*` endpoints.
    - `verify_container_maintenance_gate` bounded-deadline handling for absent, stopped, and unresponsive
      backend containers.
    - `researchdata_bridge` `/admin/drain`, `DALTON_BRIDGE_INTERNAL_CALL` injection vs host-task stripping,
      positive-integer `arguments.timeout`, shared deadline without `+5s` buffer, and `os.killpg` reaping.
    - `scripts/standalone/_compose_env.sh` non-default `COMPOSE_PROJECT_NAME` & TLS/IPv6 compose chains.
  Step 2:
    - Persistent startup ownership (`handoff-journal.json` `target_owner`) for both `:8943` proxy and
      `:8951` `research-host`, plus `POST /admin/{connector_id}/acquire-lock` & partial federated search.
    - Real subprocess chain (`:8960` -> `researchdata_mcp.py` -> `data_gateway.py`, direct `gp_search.py`)
      against slow upstream stub: shared deadline, process-group cleanup, zero orphan children, and
      immediate `in_flight == 0` on `:8943`, `:8951`, and `:8960`.
    - Real subprocess `pause_host_direct` / `DALTON_HOST_DIRECT_PAUSED=1` isolation: external host-direct
      rejected with `maintenance_host_direct_paused`, while `DALTON_BRIDGE_INTERNAL_CALL=1` and
      `DALTON_OPS_CONTROLLED_PROBE=1` succeed.
    - Real `scripts/standalone/package_release.sh` export + out-of-package `check_core_release_package.py`
      zero-hit gate across migrated public dirs, forbidden paths, migrated Core artifacts, and whole-export
      commercial class/domain terms.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from email.message import Message
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from typing import Any
from unittest.mock import create_autospec

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from src.dalton.app.router import router as dalton_router
from src.dalton.capabilities import (
    BATCH3_CUTOVER_BRIDGE_ROUTES,
    CapabilityId,
    effective_capabilities,
    find_capability_env_conflicts,
    is_batch3_connectors_enabled,
    is_bridge_ingest_migrated_to_connector,
    is_bridge_route_migrated_to_connector,
    load_deployment_capability_env,
)
from src.dalton.maintenance_gate import (
    MAINTENANCE_EXCLUSIVE_LOCK_SQL,
    MAINTENANCE_SHARED_LOCK_SQL,
    MaintenanceAdmissionPausedError,
    aupdate_maintenance_state_under_db_lock,
    enforce_maintenance_admission_for_subagent_in_txn,
    enforce_maintenance_admission_in_txn,
    read_maintenance_state,
    verify_container_maintenance_gate,
    write_maintenance_state_atomic,
)
from src.dalton.profile import DeploymentProfile
from src.dalton.research_host.__main__ import load_shared_and_host_env
from src.dalton.research_host.app import ResearchHostConfig, ResearchHostRuntime, create_app

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON_DIR = REPO_ROOT / "scripts" / "dalton"
RESEARCH_RUNTIME_DIR = SCRIPTS_DALTON_DIR / "research_runtime"


def _load_script_module(name: str, path: Path) -> Any:
    if str(SCRIPTS_DALTON_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DALTON_DIR))
    if str(RESEARCH_RUNTIME_DIR) not in sys.path:
        sys.path.insert(0, str(RESEARCH_RUNTIME_DIR))
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _clean_cap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ):
        if k.startswith(("DALTON_", "APP_ENV", "PYTHON_DOTENV", "GUIDEPOINT_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "0")


# ---------------------------------------------------------------------------
# Simulated PG readers-writer transaction advisory lock (for real concurrency race testing)
# ---------------------------------------------------------------------------


class _PgAdvisoryLockBus:
    """Faithful async readers-writer transaction lock matching PG advisory xact locks."""

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._shared_holders: int = 0
        self._exclusive_held: bool = False
        self._exclusive_waiters: int = 0
        self.observed_sql: list[str] = []

    async def acquire_shared(self) -> None:
        async with self._cond:
            while self._exclusive_held or self._exclusive_waiters > 0:
                await self._cond.wait()
            self._shared_holders += 1

    async def release_shared(self) -> None:
        async with self._cond:
            self._shared_holders = max(0, self._shared_holders - 1)
            self._cond.notify_all()

    async def acquire_exclusive(self) -> None:
        async with self._cond:
            self._exclusive_waiters += 1
            try:
                while self._exclusive_held or self._shared_holders > 0:
                    await self._cond.wait()
                self._exclusive_held = True
            finally:
                self._exclusive_waiters -= 1

    async def release_exclusive(self) -> None:
        async with self._cond:
            self._exclusive_held = False
            self._cond.notify_all()


class _FakePgTxnConn:
    """Async connection exercising real SQL lock strings & commit/rollback release semantics."""

    def __init__(self, bus: _PgAdvisoryLockBus, ledger: dict[str, Any]) -> None:
        self._bus = bus
        self._ledger = ledger
        self._held_modes: list[str] = []
        self._pending_writes: list[tuple[str, Any]] = []
        self.rolled_back: bool = False
        self.committed: bool = False

    def stage_write(self, key: str, value: Any) -> None:
        self._pending_writes.append((key, value))

    @asynccontextmanager
    async def transaction(self):
        self._held_modes = []
        self._pending_writes = []
        try:
            yield self
        except BaseException:
            self.rolled_back = True
            self._pending_writes.clear()
            raise
        else:
            for k, v in self._pending_writes:
                self._ledger[k] = v
            self.committed = True
        finally:
            for mode in reversed(self._held_modes):
                if mode == "shared":
                    await self._bus.release_shared()
                elif mode == "exclusive":
                    await self._bus.release_exclusive()
            self._held_modes.clear()

    @asynccontextmanager
    async def cursor(self, **_kwargs: Any):
        conn = self

        class _Cur:
            rowcount = 1

            async def execute(self, sql: str, _params: Any = None) -> None:
                norm = " ".join(str(sql).split())
                conn._bus.observed_sql.append(norm)
                if "pg_advisory_xact_lock_shared" in norm:
                    assert MAINTENANCE_SHARED_LOCK_SQL in norm
                    await conn._bus.acquire_shared()
                    conn._held_modes.append("shared")
                elif "pg_advisory_xact_lock(" in norm:
                    assert MAINTENANCE_EXCLUSIVE_LOCK_SQL in norm
                    await conn._bus.acquire_exclusive()
                    conn._held_modes.append("exclusive")

            async def fetchone(self) -> Any:
                return {"workspace_id": "ws-test-001", "generation": 1}

        yield _Cur()


# ===========================================================================
# STEP 1 ACCEPTANCE TESTS
# ===========================================================================


@pytest.mark.asyncio
async def test_step1_three_way_capability_env_and_route_migrated_consistency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend, research-host, and legacy bridge share one capability env & route_migrated verdict."""
    _clean_cap_env(monkeypatch)
    bridge_mod = _load_script_module(
        "_test_b3b_bridge_step1", SCRIPTS_DALTON_DIR / "researchdata_bridge.py"
    )

    repo_env = tmp_path / "repo.env"
    host_env = tmp_path / "research-host.env"
    bridge_env = tmp_path / "researchdata-bridge.env"

    # Pre-cutover Everflow repo .env (no DALTON_RESEARCH_HOST_URL, SALES_NOTES disabled)
    repo_env.write_text(
        "\n".join(
            [
                "APP_ENV=dalton",
                "DALTON_PROFILE=everflow",
                "DALTON_HOST_PLATFORM=macos",
                "DALTON_CAP_SALES_NOTES=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # Service env attempts to inject forbidden control keys -> MUST be ignored and flagged as conflict
    host_env.write_text(
        "\n".join(
            [
                "DALTON_CAP_SALES_NOTES=true",
                "DALTON_PROFILE=core",
                "DALTON_RESEARCH_HOST_TOKEN=rh-tok-1",
                "DALTON_RESEARCH_ADMIN_TOKEN=rh-adm-1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    bridge_env.write_text(
        "\n".join(
            [
                "DALTON_CAP_SALES_NOTES=true",
                "DALTON_RESEARCH_HOST_URL=http://should-be-ignored:8951",
                "DALTON_BRIDGE_TOKEN=br-tok-1",
                "DALTON_BRIDGE_ADMIN_TOKEN=br-adm-1",
                "DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8943/mcp",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # 1. Conflicts detector flags control keys in service env, plist overrides, and bad bridge env path,
    #    while ALLOWING canonical resolved DALTON_BRIDGE_ENV_FILE.
    conflicts = find_capability_env_conflicts(
        repo_env_file=repo_env,
        service_env_file=bridge_env,
        environ={"DALTON_BRIDGE_ENV_FILE": str(bridge_env)},
        extra_sources={
            "plist:bridge": {
                "DALTON_BRIDGE_ENV_FILE": str(bridge_env),
                "DALTON_GUIDEPOINT_MCP_URL": "http://127.0.0.1:8943/mcp",
            },
            "plist:bad": {
                "DALTON_BRIDGE_ENV_FILE": str(tmp_path / "wrong.env"),
                "DALTON_CAP_SALES_NOTES": "true",
            },
        },
        expected_bridge_env_file=bridge_env,
    )
    reasons = {(c["source"], c["key"], c["reason"]) for c in conflicts}
    assert any(r[2] == "control_key_in_service_env" and r[1] == "DALTON_CAP_SALES_NOTES" for r in reasons)
    assert any(r[2] == "control_key_in_service_env" and r[1] == "DALTON_RESEARCH_HOST_URL" for r in reasons)
    assert any(r[2] == "guidepoint_url_env_override" for r in reasons)
    assert any(r[0] == "plist:bad" and r[2] == "unexpected_bridge_env_file_path" for r in reasons)
    assert any(r[0] == "plist:bad" and r[2] == "control_key_override" for r in reasons)
    # Canonical resolved DALTON_BRIDGE_ENV_FILE must NOT be flagged
    assert not any(
        r[0] in ("process_env", "plist:bridge") and r[2] == "unexpected_bridge_env_file_path"
        for r in reasons
    )

    # 2. Three-way effective capabilities & cutover verdict match BEFORE cutover
    backend_env = load_deployment_capability_env(repo_env_file=repo_env, environ={})
    host_loaded_env: dict[str, str] = {}
    load_shared_and_host_env(env_file=str(host_env), repo_env_file=str(repo_env), environ=host_loaded_env)
    bridge_merged_env = load_deployment_capability_env(
        repo_env_file=repo_env, service_env_file=bridge_env, environ={}
    )

    caps_backend = effective_capabilities(environ=backend_env)
    caps_host = effective_capabilities(environ=host_loaded_env)
    caps_bridge = effective_capabilities(environ=bridge_merged_env)
    assert caps_backend == caps_host == caps_bridge
    assert CapabilityId.SALES_NOTES not in caps_backend
    assert is_batch3_connectors_enabled(environ=backend_env) is False
    assert is_batch3_connectors_enabled(environ=host_loaded_env) is False
    assert is_batch3_connectors_enabled(environ=bridge_merged_env) is False
    assert is_bridge_route_migrated_to_connector("guidepoint", environ=bridge_merged_env) is False
    assert is_bridge_ingest_migrated_to_connector("wiki", environ=bridge_merged_env) is False

    # 3. Post-cutover: adding DALTON_RESEARCH_HOST_URL to shared repo .env flips ALL 3 ends & closes bridge routes
    repo_env.write_text(
        repo_env.read_text(encoding="utf-8") + "DALTON_RESEARCH_HOST_URL=http://host.docker.internal:8951\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DALTON_REPO_ENV_FILE", str(repo_env))
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))
    cfg = bridge_mod.BridgeConfig()
    backend_post = load_deployment_capability_env(repo_env_file=repo_env, environ={})
    host_post: dict[str, str] = {}
    load_shared_and_host_env(env_file=host_env, repo_env_file=repo_env, environ=host_post)
    assert is_batch3_connectors_enabled(environ=backend_post) is True
    assert is_batch3_connectors_enabled(environ=host_post) is True
    assert is_batch3_connectors_enabled(environ=cfg.capability_env) is True
    for route_id in BATCH3_CUTOVER_BRIDGE_ROUTES:
        assert is_bridge_route_migrated_to_connector(route_id, environ=cfg.capability_env) is True
    assert is_bridge_route_migrated_to_connector("alphaengine", environ=cfg.capability_env) is False
    assert is_bridge_route_migrated_to_connector("discord-history", environ=cfg.capability_env) is False
    assert is_bridge_ingest_migrated_to_connector("wiki", environ=cfg.capability_env) is True
    assert is_bridge_ingest_migrated_to_connector("sales-notes", environ=cfg.capability_env) is True

    bridge = bridge_mod.ResearchDataBridge(cfg)
    mcp_resp = await bridge.handle_tools_call(
        1,
        {"name": "research", "arguments": {"route": "guidepoint", "operation": "search_library", "query": "cloud"}},
    )
    assert mcp_resp["result"]["isError"] is True
    mcp_body = json.loads(mcp_resp["result"]["content"][0]["text"])
    assert mcp_body["error_code"] == "route_migrated"

    w_code, w_body = await bridge.handle_wiki_ingest({"target_type": "company", "target": "NVDA", "content_type": "note", "content": "hi"})
    assert w_code == 410 and w_body["error_code"] == "route_migrated"

    s_code, s_body = await bridge.handle_sales_notes_ingest({"institution": "MS", "title": "Note", "content": "hi"})
    assert s_code == 410 and s_body["error_code"] == "route_migrated"


@pytest.mark.asyncio
async def test_step1_pg_advisory_lock_commit_boundary_and_maintenance_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG shared/exclusive advisory locks serialize START/data-task commit vs host pause & block post-pause callers."""
    _clean_cap_env(monkeypatch)
    state_file = tmp_path / "maintenance-admission.json"
    monkeypatch.setenv("DALTON_MAINTENANCE_ADMISSION_FILE", str(state_file))

    bus = _PgAdvisoryLockBus()
    ledger: dict[str, Any] = {"in_progress": 0, "data_task_slot": "idle"}

    in_before_commit_both = asyncio.Event()
    release_commits = asyncio.Event()
    entered_count = 0

    async def _run_start_txn() -> None:
        nonlocal entered_count
        conn = _FakePgTxnConn(bus, ledger)
        async with conn.transaction():
            conn.stage_write("in_progress", 1)
            await enforce_maintenance_admission_in_txn(
                conn, workspace_id="ws-normal", state_file=state_file
            )
            entered_count += 1
            if entered_count == 2:
                in_before_commit_both.set()
            await release_commits.wait()

    async def _run_data_task_claim_txn() -> None:
        nonlocal entered_count
        conn = _FakePgTxnConn(bus, ledger)
        async with conn.transaction():
            await enforce_maintenance_admission_in_txn(
                conn, context="data_task_claim", state_file=state_file
            )
            conn.stage_write("data_task_slot", "running")
            entered_count += 1
            if entered_count == 2:
                in_before_commit_both.set()
            await release_commits.wait()

    t_start = asyncio.create_task(_run_start_txn())
    t_claim = asyncio.create_task(_run_data_task_claim_txn())
    await asyncio.wait_for(in_before_commit_both.wait(), timeout=2.0)

    # Host tries to pause under PG exclusive advisory lock -> MUST block while both transactions are uncommitted
    pause_conn = _FakePgTxnConn(bus, ledger)
    t_pause = asyncio.create_task(
        aupdate_maintenance_state_under_db_lock(
            paused=True,
            allow_workspace_ids=(),
            pause_host_direct=True,
            reason="cutover_step1",
            operation="switch",
            state_file=state_file,
            conn=pause_conn,
        )
    )
    await asyncio.sleep(0.05)
    assert not t_pause.done(), "Host exclusive lock MUST block until open shared-lock transactions commit"
    assert read_maintenance_state(state_file).paused is False

    # Let both in-flight transactions commit -> idle check now sees in_progress==1 and data_task_slot=='running'
    release_commits.set()
    await asyncio.gather(t_start, t_claim)
    paused_state = await asyncio.wait_for(t_pause, timeout=2.0)
    assert paused_state.paused is True
    assert paused_state.revision == 1
    assert paused_state.pause_host_direct is True
    assert ledger["in_progress"] == 1
    assert ledger["data_task_slot"] == "running"

    # After pause lands: BOTH normal workspace and test workspace are rolled back with 503 when allow_workspace_ids=[]
    for ws in ("ws-normal", "ws-test-001"):
        c_post = _FakePgTxnConn(bus, ledger)
        with pytest.raises(MaintenanceAdmissionPausedError) as exc_info:
            async with c_post.transaction():
                c_post.stage_write("should_not_commit", True)
                await enforce_maintenance_admission_in_txn(
                    c_post, workspace_id=ws, state_file=state_file
                )
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["error_code"] == "maintenance_in_progress"
        assert c_post.rolled_back is True
        assert "should_not_commit" not in ledger

    # Subagent txn guard also rolls back with 503
    c_sub = _FakePgTxnConn(bus, ledger)
    with pytest.raises(MaintenanceAdmissionPausedError) as sub_exc:
        async with c_sub.transaction():
            await enforce_maintenance_admission_for_subagent_in_txn(
                c_sub, thread_id="thr-1", state_file=state_file
            )
    assert sub_exc.value.status_code == 503
    assert c_sub.rolled_back is True

    # Backend HTTP status & probe endpoints reflect paused state & enforce X-Service-Token
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "svc-tok-at-least-32-characters-long-0001")
    app = FastAPI()
    app.include_router(dalton_router)
    client = TestClient(app)

    r_unauth = client.get("/api/v1/dalton/maintenance/admission")
    assert r_unauth.status_code == 401

    hdrs = {"X-Service-Token": "svc-tok-at-least-32-characters-long-0001"}
    r_status = client.get("/api/v1/dalton/maintenance/admission", headers=hdrs)
    assert r_status.status_code == 200
    assert r_status.json()["paused"] is True and r_status.json()["revision"] == 1

    r_probe = client.post("/api/v1/dalton/maintenance/admission/probe", headers=hdrs, json={})
    assert r_probe.status_code == 503
    assert r_probe.json()["detail"]["error_code"] == "maintenance_in_progress"

    # Allowlisting ws-test-001 admits ONLY ws-test-001 while ws-normal remains 503
    st_allow = write_maintenance_state_atomic(
        {
            "version": 1,
            "revision": 2,
            "paused": True,
            "pause_host_direct": False,
            "allow_workspace_ids": ["ws-test-001"],
            "reason": "step7_verify",
            "operation": "switch",
            "rehearsal_keep_paused_until": None,
            "updated_at": "2026-11-26T00:00:00+00:00",
        },
        state_file=state_file,
    )
    assert st_allow.revision == 2
    r_probe_ok = client.post(
        "/api/v1/dalton/maintenance/admission/probe",
        headers=hdrs,
        json={"workspace_id": "ws-test-001"},
    )
    assert r_probe_ok.status_code == 200 and r_probe_ok.json()["admitted"] is True
    r_probe_blocked = client.post(
        "/api/v1/dalton/maintenance/admission/probe",
        headers=hdrs,
        json={"workspace_id": "ws-normal"},
    )
    assert r_probe_blocked.status_code == 503

    # Verify wiki_tools & ingest_tools reject confirm/write calls under maintenance gate
    from src.dalton.tools.ingest_tools import call_bridge_sales_notes_ingest
    from src.dalton.tools.wiki_tools import call_bridge_wiki_ingest

    w_code, w_res = await call_bridge_wiki_ingest(
        {"dry_run": False, "ingested_via": {"workspace_id": "ws-normal"}}
    )
    assert w_code == 503 and w_res["error_code"] == "maintenance_in_progress"
    s_code, s_res = await call_bridge_sales_notes_ingest(
        {"dry_run": False, "ingested_via": {"workspace_id": "ws-normal"}}
    )
    assert s_code == 503 and s_res["error_code"] == "maintenance_in_progress"


def test_step1_verify_container_maintenance_gate_absent_and_unresponsive() -> None:
    """Container gate verification treats absent/stopped or unresponsive containers as `unavailable` within deadline."""
    fake_now = [0.0]

    def _clock() -> float:
        return fake_now[0]

    def _sleep(sec: float) -> None:
        fake_now[0] += sec

    # 1. Container absent -> immediate `unavailable`
    docker_absent = create_autospec(
        lambda args, timeout_s: (1, "", "No such object"),
        return_value=(1, "", "No such object"),
    )
    res_absent = verify_container_maintenance_gate(
        expected_revision=3,
        deadline_s=10.0,
        docker_runner=docker_absent,
        clock=_clock,
        sleep_fn=_sleep,
    )
    assert res_absent.status == "unavailable"
    assert res_absent.container_available is False
    assert res_absent.reason == "container_absent"

    # 2. Container running, but `:8000` API refuses connection / times out -> `unavailable` once deadline expires
    fake_now[0] = 0.0

    def _docker_running(args: list[str], _timeout_s: float) -> tuple[int, str, str]:
        if args[0] == "inspect":
            return 0, "true\n", ""
        return 0, json.dumps({"revision": 3, "paused": True}), ""

    def _http_unreachable(*_args: Any, **_kwargs: Any) -> tuple[int | None, dict[str, Any] | None, str]:
        return None, None, "ConnectionRefusedError: refused"

    res_unresp = verify_container_maintenance_gate(
        expected_revision=3,
        deadline_s=3.0,
        poll_interval_s=1.0,
        docker_runner=_docker_running,
        http_requester=_http_unreachable,
        clock=_clock,
        sleep_fn=_sleep,
    )
    assert res_unresp.status == "unavailable"
    assert res_unresp.container_available is False
    assert res_unresp.elapsed_s == pytest.approx(3.0)

    # 3. Container running with matching revision & 503 maintenance_in_progress probe -> `verified`
    fake_now[0] = 0.0

    def _http_paused(*_args: Any, **_kwargs: Any) -> tuple[int | None, dict[str, Any] | None, str]:
        return 503, {"detail": {"error_code": "maintenance_in_progress", "revision": 3}}, ""

    res_ok = verify_container_maintenance_gate(
        expected_revision=3,
        deadline_s=5.0,
        docker_runner=_docker_running,
        http_requester=_http_paused,
        clock=_clock,
        sleep_fn=_sleep,
    )
    assert res_ok.status == "verified"
    assert res_ok.container_available is True
    assert res_ok.container_revision == 3
    assert res_ok.probe_status_code == 503


@pytest.mark.asyncio
async def test_step1_bridge_drain_internal_call_env_and_pgroup_timeout_reap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge injects DALTON_BRIDGE_INTERNAL_CALL, host tasks strip it, drain returns 503, and timeout kills pgroup."""
    _clean_cap_env(monkeypatch)
    benv = _load_script_module(
        "_test_b3b_benv_step1", SCRIPTS_DALTON_DIR / "bridge_subprocess_env.py"
    )
    bridge_mod = _load_script_module(
        "_test_b3b_bridge_pgroup", SCRIPTS_DALTON_DIR / "researchdata_bridge.py"
    )

    # 1. RESEARCH_RUNTIME_ENV_KEYS passes internal marker; host task builder explicitly strips it
    rt_env = benv.build_research_runtime_subprocess_env(
        {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        runtime_env={
            "DALTON_BRIDGE_INTERNAL_CALL": "1",
            "DALTON_REQUEST_DEADLINE_EPOCH_MS": "9999999999999",
        },
    )
    assert rt_env.get("DALTON_BRIDGE_INTERNAL_CALL") == "1"
    assert rt_env.get("DALTON_REQUEST_DEADLINE_EPOCH_MS") == "9999999999999"

    host_task_env = benv.build_host_task_subprocess_env(
        {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "DALTON_BRIDGE_INTERNAL_CALL": "1",
            "DALTON_REQUEST_DEADLINE_EPOCH_MS": "9999999999999",
            "DALTON_OPS_CONTROLLED_PROBE": "1",
        },
        plain_env={"DALTON_BRIDGE_INTERNAL_CALL": "1", "DALTON_OPS_CONTROLLED_PROBE": "1"},
    )
    assert "DALTON_BRIDGE_INTERNAL_CALL" not in host_task_env
    assert "DALTON_REQUEST_DEADLINE_EPOCH_MS" not in host_task_env
    assert "DALTON_OPS_CONTROLLED_PROBE" not in host_task_env

    # 2. Bridge compute_timeout allows positive int timeout=1 without 5s floor uplift
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(tmp_path / "missing.env"))
    cfg = bridge_mod.BridgeConfig()
    cfg.python_bin = sys.executable
    bridge = bridge_mod.ResearchDataBridge(cfg)
    assert bridge.compute_timeout("research", {"route": "guidepoint", "timeout": 1}) == 1
    assert bridge.compute_timeout("research", {"route": "guidepoint", "timeout": 2}) == 2

    # 3. Drain toggles reject ingest with 503 service_draining & report counts
    st = bridge.set_research_draining(True)
    assert st["draining"] is True and st["inflight"] == 0 and st["queued"] == 0 and st["ingest_inflight"] == 0
    w_code, w_body = await bridge.handle_wiki_ingest({"target_type": "company", "target": "NVDA", "content_type": "note", "content": "x"})
    assert w_code == 503 and w_body["error_code"] == "service_draining"
    bridge.set_research_draining(False)

    # 4. Real subprocess group kill via os.killpg with zero +5s buffer and zero orphan grandchildren
    pid_file = tmp_path / "grandchild.pid"
    slow_mcp = tmp_path / "slow_mcp.py"
    slow_mcp.write_text(
        f"""
import os, subprocess, sys, time
assert os.environ.get("DALTON_BRIDGE_INTERNAL_CALL") == "1"
assert os.environ.get("DALTON_REQUEST_DEADLINE_EPOCH_MS")
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
with open({str(pid_file)!r}, "w") as fh:
    fh.write(f"{{os.getpid()}},{{child.pid}}\\n")
sys.stdin.readline()
time.sleep(30)
""",
        encoding="utf-8",
    )
    cfg.mcp_script = slow_mcp
    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await bridge._run_stdio_rpc(
            [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}],
            timeout_s=0.6,
        )
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0, f"Expected ~0.6s timeout without +5s buffer, took {elapsed:.2f}s"

    # Both child and grandchild in the killed process group must be gone
    assert pid_file.is_file()
    child_pid, grand_pid = [int(x) for x in pid_file.read_text(encoding="utf-8").strip().split(",")]
    await asyncio.sleep(0.15)
    for pid in (child_pid, grand_pid):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert bridge.research_drain_status()["inflight"] == 0


def test_step1_standalone_compose_env_non_default_project_and_tls_modes(
    tmp_path: Path,
) -> None:
    """`scripts/standalone/_compose_env.sh` resolves non-default project names & TLS/IPv6 compose chains."""
    helper = REPO_ROOT / "scripts" / "standalone" / "_compose_env.sh"
    assert helper.is_file()

    # Check bash syntax of all modified standalone scripts
    for script in (
        helper,
        REPO_ROOT / "scripts" / "standalone" / "init.sh",
        REPO_ROOT / "scripts" / "standalone" / "upgrade.sh",
        REPO_ROOT / "scripts" / "standalone" / "package_release.sh",
    ):
        res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
        assert res.returncode == 0, f"bash -n failed for {script}: {res.stderr}"

    cases = [
        ("my-custom-inst", "loopback", "localhost", "", ["docker-compose.yml", "docker-compose.core.yml"]),
        ("tls-v4-inst", "internal", "dalton.example.com", "0.0.0.0", ["docker-compose.yml", "docker-compose.core.yml", "deploy/standalone/docker-compose.tls.yml"]),
        ("tls-v6-inst", "auto", "dalton.example.com", "[::]", ["docker-compose.yml", "docker-compose.core.yml", "deploy/standalone/docker-compose.tls.ipv6.yml"]),
    ]
    for proj, tls_mode, domain, bind, expected_suffixes in cases:
        env_f = tmp_path / f"{proj}.env"
        env_f.write_text(
            f"COMPOSE_PROJECT_NAME={proj}\nDALTON_TLS_MODE={tls_mode}\nDOMAIN={domain}\nDALTON_EDGE_TLS_BIND={bind}\n",
            encoding="utf-8",
        )
        cmd = (
            f'source "{helper}" && '
            f'standalone_load_compose_env "{REPO_ROOT}" "{env_f}" && '
            'printf "%s\\n%s\\n" "$PROJECT_NAME" "$COMPOSE_FILES_JOINED"'
        )
        out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, check=True)
        lines = out.stdout.strip().splitlines()
        assert lines[0] == proj
        resolved_files = lines[1].split(":")
        assert [str(Path(f).relative_to(REPO_ROOT)) for f in resolved_files] == expected_suffixes


# ===========================================================================
# STEP 2 ACCEPTANCE TESTS
# ===========================================================================


@pytest.mark.asyncio
async def test_step2_guidepoint_startup_ownership_and_admin_acquire_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both `:8943` and `:8951` read `target_owner` first; standby skips lock & `acquire-lock` hands over cleanly."""
    _clean_cap_env(monkeypatch)
    gp_lock_mod = _load_script_module(
        "_test_b3b_gp_lock", RESEARCH_RUNTIME_DIR / "guidepoint_lock.py"
    )
    proxy_mod = _load_script_module(
        "_test_b3b_gp_proxy", RESEARCH_RUNTIME_DIR / "scripts" / "guidepoint_mcp_proxy.py"
    )
    gp_conn_mod = _load_script_module(
        "_test_b3b_gp_conn", RESEARCH_RUNTIME_DIR / "host_connectors" / "guidepoint.py"
    )

    journal_file = tmp_path / "handoff-journal.json"
    token_file = tmp_path / "config" / "guidepoint_mcp_tokens.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(
        json.dumps(
            {
                "access_token": "tok-valid-1",
                "refresh_token": "ref-valid-1",
                "obtained_at": time.time(),
                "expires_in": 600,
            }
        ),
        encoding="utf-8",
    )

    # 1. target_owner = legacy_proxy_8943 -> :8943 acquires lock, :8951 enters standby without touching lock/tokens
    gp_lock_mod.set_target_owner(gp_lock_mod.OWNER_LEGACY_PROXY, ownership_file=journal_file)
    monkeypatch.setenv("DALTON_GUIDEPOINT_OWNERSHIP_FILE", str(journal_file))
    monkeypatch.setenv("GUIDEPOINT_TOKENS_FILE", str(token_file))
    monkeypatch.setenv("GUIDEPOINT_CLIENT_ID", "cid")
    monkeypatch.setenv("GUIDEPOINT_CLIENT_SECRET", "csec")

    acquired_legacy = proxy_mod.initialize_runtime(ownership_file=journal_file, token_store=token_file)
    assert acquired_legacy is True
    assert proxy_mod.health_payload()["owner_lock_held"] is True
    assert proxy_mod.health_payload()["standby"] is False

    try:
        # Start research-host runtime with host_connectors -> Guidepoint starts in standby (zero lock conflict!)
        rt = ResearchHostRuntime(
            ResearchHostConfig(
                data_root=tmp_path,
                mcp_token="mcp-s2",
                admin_token="adm-s2",
                profile=DeploymentProfile.EVERFLOW,
                extra_modules=["host_connectors"],
                extra_python_paths=[str(RESEARCH_RUNTIME_DIR)],
                environ={
                    **os.environ,
                    "DALTON_GUIDEPOINT_OWNERSHIP_FILE": str(journal_file),
                    "GUIDEPOINT_TOKENS_FILE": str(token_file),
                },
            ),
            capabilities_provider=lambda: frozenset({CapabilityId.COMPANY_WIKI, CapabilityId.GUIDEPOINT}),
        )
        app = create_app(runtime=rt)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8951") as client:
            hz = (await client.get("/healthz")).json()
            assert hz["probes"]["guidepoint"]["owner_lock_held"] is False
            assert hz["probes"]["guidepoint"]["standby"] is True

            # Direct /mcp/guidepoint returns 503 lock_not_acquired while standby
            gp_call = await client.post(
                "/mcp/guidepoint",
                headers={"Authorization": "Bearer mcp-s2"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "search_library", "arguments": {"query": "cloud", "size": 1}},
                },
            )
            assert gp_call.status_code == 503
            gp_payload = gp_call.json()["result"]["structuredContent"]
            assert gp_payload["error_code"] == "lock_not_acquired"

            # Federated all.search returns partial with guidepoint in source_status while wiki succeeds
            fed_call = await client.post(
                "/mcp/federated",
                headers={"Authorization": "Bearer mcp-s2"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "search", "arguments": {"query": "cloud"}},
                },
            )
            assert fed_call.status_code == 200
            fed_sc = fed_call.json()["result"]["structuredContent"]
            assert fed_sc["status"] == "partial"
            assert fed_sc["source_status"]["company-wiki"]["ok"] is True
            assert fed_sc["source_status"]["guidepoint"]["ok"] is False

            # /admin/guidepoint/acquire-lock refuses while target_owner is still legacy_proxy_8943
            refused = await client.post(
                "/admin/guidepoint/acquire-lock",
                headers={"Authorization": "Bearer adm-s2"},
                json={},
            )
            assert refused.status_code == 409
            assert refused.json()["error_code"] == "target_owner_mismatch"

            # Release :8943 lock & switch target_owner -> research_host_8951 -> acquire-lock succeeds!
            proxy_mod.release_owner_lock()
            gp_lock_mod.set_target_owner(gp_lock_mod.OWNER_RESEARCH_HOST, ownership_file=journal_file)

            acq = await client.post(
                "/admin/guidepoint/acquire-lock",
                headers={"Authorization": "Bearer adm-s2"},
                json={"timeout_s": 2.0},
            )
            assert acq.status_code == 200
            assert acq.json()["owner_lock_held"] is True
            assert acq.json()["standby"] is False

            hz2 = (await client.get("/healthz")).json()
            assert hz2["probes"]["guidepoint"]["owner_lock_held"] is True
            assert hz2["probes"]["guidepoint"]["standby"] is False

            # Starting :8943 now sees target_owner=research_host_8951 and enters standby without taking lock
            acquired_legacy_again = proxy_mod.initialize_runtime(
                ownership_file=journal_file, token_store=token_file
            )
            assert acquired_legacy_again is False
            assert proxy_mod.health_payload()["standby"] is True
            assert proxy_mod.health_payload()["owner_lock_held"] is False
        await rt.shutdown()
    finally:
        proxy_mod.release_owner_lock()
    _ = gp_conn_mod


@pytest.mark.asyncio
async def test_step2_real_subprocess_chain_shared_deadline_and_inflight_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow upstream + real subprocess chain (:8960 -> researchdata_mcp -> data_gateway),
    plus :8943 proxy and :8951 research-host GuidepointConnector: shared deadline overrides 120s/60s
    with no +5s/+10s buffer, reaps full child process group, and returns in_flight to 0."""
    _clean_cap_env(monkeypatch)
    gp_lock_mod = _load_script_module(
        "_test_b3b_gp_lock_slow", RESEARCH_RUNTIME_DIR / "guidepoint_lock.py"
    )
    proxy_mod = _load_script_module(
        "_test_b3b_gp_proxy_slow", RESEARCH_RUNTIME_DIR / "scripts" / "guidepoint_mcp_proxy.py"
    )
    gp_conn_mod = _load_script_module(
        "_test_b3b_gp_conn_slow", RESEARCH_RUNTIME_DIR / "host_connectors" / "guidepoint.py"
    )
    bridge_mod = _load_script_module(
        "_test_b3b_bridge_slow", SCRIPTS_DALTON_DIR / "researchdata_bridge.py"
    )

    # 1. Real subprocess chain (:8960 -> researchdata_mcp.py -> data_gateway.py -> slow upstream)
    shim_dir = tmp_path / "pyshim_slow"
    shim_dir.mkdir()
    trace_file = tmp_path / "slow_trace.jsonl"
    (shim_dir / "sitecustomize.py").write_text(
        f"""
import json, os, time, urllib.request
_orig_open = urllib.request.OpenerDirector.open
_TRACE = {str(trace_file)!r}
def _slow_open(self, fullurl, data=None, timeout=None):
    url = fullurl.full_url if hasattr(fullurl, "full_url") else str(fullurl)
    if "127.0.0.1:18943" in url:
        rec = {{
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "pgid": os.getpgid(0),
            "deadline_env": os.environ.get("DALTON_REQUEST_DEADLINE_EPOCH_MS"),
            "internal_call": os.environ.get("DALTON_BRIDGE_INTERNAL_CALL"),
            "timeout_hdr": fullurl.get_header("X-dalton-request-timeout-ms") if hasattr(fullurl, "get_header") else None,
            "urlopen_timeout": timeout,
        }}
        with open(_TRACE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\\n")
        time.sleep(30.0)
    return _orig_open(self, fullurl, data=data, timeout=timeout)
urllib.request.OpenerDirector.open = _slow_open
""",
        encoding="utf-8",
    )
    py_wrapper = tmp_path / "python_with_shim.sh"
    py_wrapper.write_text(
        f"#!/bin/sh\nexport PYTHONPATH={str(shim_dir)}:${{PYTHONPATH:-}}\nexec {sys.executable} \"$@\"\n",
        encoding="utf-8",
    )
    py_wrapper.chmod(0o755)

    bridge_env = tmp_path / "researchdata-bridge.env"
    bridge_env.write_text(
        "DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:18943/mcp\n"
        "DALTON_BRIDGE_TOKEN=br-tok\n"
        "DALTON_BRIDGE_ADMIN_TOKEN=br-adm\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))
    monkeypatch.setenv("DALTON_GUIDEPOINT_MCP_URL", "http://127.0.0.1:18943/mcp")
    monkeypatch.setenv("DALTON_RESEARCH_CODE", str(RESEARCH_RUNTIME_DIR))
    monkeypatch.setenv("DALTON_RESEARCH_HOME", str(tmp_path))
    cfg = bridge_mod.BridgeConfig()
    cfg.python_bin = str(py_wrapper)
    bridge = bridge_mod.ResearchDataBridge(cfg)

    t0 = time.monotonic()
    resp = await bridge.handle_tools_call(
        1,
        {
            "name": "research",
            "arguments": {
                "route": "guidepoint",
                "operation": "search_library",
                "query": "cloud",
                "limit": 1,
                "timeout": 3,
            },
        },
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 5.5, f"Shared 3s deadline exceeded (must not add +5s/+10s buffer): {elapsed:.2f}s"
    assert resp["result"]["isError"] is True
    assert bridge.research_drain_status()["inflight"] == 0

    # Verify data_gateway.py actually ran, received shared deadline & internal flag, and was reaped by os.killpg
    assert trace_file.is_file(), "data_gateway.py grandchild never reached upstream"
    records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert records, "No upstream trace recorded"
    rec0 = records[0]
    assert rec0["internal_call"] == "1"
    assert rec0["deadline_env"] is not None and int(rec0["deadline_env"]) > 0
    assert rec0["timeout_hdr"] is not None and int(rec0["timeout_hdr"]) <= 3500
    assert rec0["urlopen_timeout"] is not None and float(rec0["urlopen_timeout"]) <= 3.5
    await asyncio.sleep(0.15)
    with pytest.raises(ProcessLookupError):
        os.kill(int(rec0["pid"]), 0)

    # 2. Verify :8943 guidepoint_mcp_proxy.ProxyHandler enforces shared deadline (not 120s),
    # strips arguments.timeout, returns 504 upstream_timeout, and restores in_flight == 0
    journal_file = tmp_path / "handoff-journal.json"
    token_file = tmp_path / "config" / "guidepoint_mcp_tokens.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(
        json.dumps(
            {
                "access_token": "tok-slow",
                "refresh_token": "ref-slow",
                "obtained_at": time.time(),
                "expires_in": 600,
            }
        ),
        encoding="utf-8",
    )
    gp_lock_mod.set_target_owner(gp_lock_mod.OWNER_LEGACY_PROXY, ownership_file=journal_file)
    monkeypatch.setenv("DALTON_GUIDEPOINT_OWNERSHIP_FILE", str(journal_file))
    monkeypatch.setenv("GUIDEPOINT_TOKENS_FILE", str(token_file))
    monkeypatch.setenv("GUIDEPOINT_CLIENT_ID", "cid")
    monkeypatch.setenv("GUIDEPOINT_CLIENT_SECRET", "csec")
    monkeypatch.setenv("GUIDEPOINT_PROXY_ADMIN_TOKEN", "gp-adm-1")

    proxy_mod.initialize_runtime(ownership_file=journal_file, token_store=token_file)
    try:
        seen_proxy_upstream: dict[str, Any] = {}

        def _fake_proxy_urlopen(req, data=None, timeout=None, *a, **kw):
            seen_proxy_upstream["timeout"] = timeout
            seen_proxy_upstream["body"] = json.loads((req.data or b"{}").decode("utf-8"))
            raise socket.timeout("timed out")

        monkeypatch.setattr(proxy_mod.urllib.request, "urlopen", _fake_proxy_urlopen)

        req_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "search_library",
                    "arguments": {"query": "cloud", "size": 1, "timeout": 1},
                },
            }
        ).encode("utf-8")
        hdrs = Message()
        hdrs["Content-Type"] = "application/json"
        hdrs["Content-Length"] = str(len(req_body))
        hdrs["X-Dalton-Request-Timeout-Ms"] = "400"

        handler = object.__new__(proxy_mod.ProxyHandler)
        handler.command = "POST"
        handler.path = "/mcp"
        handler.headers = hdrs
        handler.rfile = io.BytesIO(req_body)
        handler.wfile = io.BytesIO()
        sent: list[tuple[int, dict[str, Any]]] = []
        handler._send_json = lambda status, payload: sent.append((status, payload))
        handler._proxy()

        assert sent and sent[0][0] == 504
        assert sent[0][1]["error_code"] == "upstream_timeout"
        assert float(seen_proxy_upstream["timeout"]) <= 0.5
        assert "timeout" not in seen_proxy_upstream["body"]["params"]["arguments"]
        assert proxy_mod.health_payload()["in_flight"] == 0
    finally:
        proxy_mod.release_owner_lock()

    # 3. Verify :8951 research-host GuidepointConnector cancels slow upstream on shared deadline
    # via asyncio.wait_for (overriding 60s default) and restores rt.in_flight == 0
    gp_lock_mod.set_target_owner(gp_lock_mod.OWNER_RESEARCH_HOST, ownership_file=journal_file)
    tm = gp_conn_mod.GuidepointTokenManager(token_file, ownership_file=journal_file)
    tm.initialize_from_ownership(timeout_s=2.0)
    try:
        async def _slow_transport_handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(10.0)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

        real_async_client = httpx.AsyncClient
        monkeypatch.setattr(
            gp_conn_mod.httpx,
            "AsyncClient",
            lambda *a, **kw: real_async_client(
                transport=httpx.MockTransport(_slow_transport_handler), **kw
            ),
        )
        gp_conn = gp_conn_mod.GuidepointConnector(
            tm, upstream_url="http://127.0.0.1:18999/mcp"
        )
        rt = ResearchHostRuntime(
            ResearchHostConfig(
                data_root=tmp_path,
                mcp_token="rh-mcp",
                admin_token="rh-adm",
                profile=DeploymentProfile.EVERFLOW,
            ),
            capabilities_provider=lambda: frozenset({CapabilityId.GUIDEPOINT}),
        )
        rt.registry.register_connector(gp_conn)
        app = create_app(runtime=rt)
        async with real_async_client(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8951"
        ) as client:
            t1 = time.monotonic()
            r_host = await client.post(
                "/mcp/guidepoint",
                headers={
                    "Authorization": "Bearer rh-mcp",
                    "X-Dalton-Request-Timeout-Ms": "400",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {
                        "name": "search_library",
                        "arguments": {"query": "cloud", "size": 1, "timeout": 1},
                    },
                },
            )
            elapsed_host = time.monotonic() - t1
            assert elapsed_host < 2.0
            assert r_host.status_code == 200
            sc = r_host.json()["result"]["structuredContent"]
            assert sc["ok"] is False and sc["error_code"] == "upstream_timeout"
            assert rt.in_flight == 0
    finally:
        tm.release_owner_lock()


def test_step2_host_direct_pause_vs_bridge_internal_and_controlled_probe(
    tmp_path: Path,
) -> None:
    """Under `pause_host_direct: true` + `DALTON_HOST_DIRECT_PAUSED=1`, external direct calls are rejected,
    while `DALTON_BRIDGE_INTERNAL_CALL=1` (via researchdata_mcp) and `DALTON_OPS_CONTROLLED_PROBE=1` succeed."""
    shim_dir = tmp_path / "pyshim_fast"
    shim_dir.mkdir()
    (shim_dir / "sitecustomize.py").write_text(
        """
import io, json, urllib.request
from email.message import Message
_orig_open = urllib.request.OpenerDirector.open
class _Resp:
    def __init__(self, body_bytes):
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self.headers["Mcp-Session-Id"] = "sess-1"
        self._buf = io.BytesIO(body_bytes)
    def read(self, n=-1):
        return self._buf.read(n)
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
def _fast_open(self, fullurl, data=None, timeout=None):
    url = fullurl.full_url if hasattr(fullurl, "full_url") else str(fullurl)
    if "127.0.0.1:18944" in url:
        raw = fullurl.data if hasattr(fullurl, "data") and fullurl.data else b"{}"
        parsed = json.loads(raw.decode("utf-8"))
        method = parsed.get("method")
        req_id = parsed.get("id", 1)
        if method == "initialize":
            reply = {"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "mock-gp", "version": "1.0"}}}
        elif method == "notifications/initialized":
            reply = {"jsonrpc": "2.0", "id": req_id, "result": {}}
        else:
            reply = {"jsonrpc": "2.0", "id": req_id, "result": {"structuredContent": {"data": [{"transcript_id": "gp-1", "transcript_name": "Cloud Call", "date": "2026-11-20"}]}, "isError": False}}
        return _Resp(json.dumps(reply).encode("utf-8"))
    return _orig_open(self, fullurl, data=data, timeout=timeout)
urllib.request.OpenerDirector.open = _fast_open
""",
        encoding="utf-8",
    )

    gate_file = tmp_path / "maintenance-admission.json"
    write_maintenance_state_atomic(
        {
            "version": 1,
            "revision": 1,
            "paused": True,
            "pause_host_direct": True,
            "allow_workspace_ids": [],
            "reason": "stage_a",
            "operation": "stage_a",
            "rehearsal_keep_paused_until": None,
            "updated_at": "2026-11-26T00:00:00+00:00",
        },
        state_file=gate_file,
    )
    bridge_env_file = tmp_path / "researchdata-bridge.env"
    bridge_env_file.write_text(
        "DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:18944/mcp\n"
        "DALTON_HOST_DIRECT_PAUSED=1\n",
        encoding="utf-8",
    )

    base_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(shim_dir),
        "DALTON_RESEARCH_CODE": str(RESEARCH_RUNTIME_DIR),
        "DALTON_RESEARCH_HOME": str(tmp_path),
        "DALTON_BRIDGE_ENV_FILE": str(bridge_env_file),
        "DALTON_MAINTENANCE_ADMISSION_FILE": str(gate_file),
    }
    dg_script = str(RESEARCH_RUNTIME_DIR / "scripts" / "data_gateway.py")
    mcp_script = str(RESEARCH_RUNTIME_DIR / "scripts" / "researchdata_mcp.py")
    gp_script = str(
        RESEARCH_RUNTIME_DIR / "skills" / "guidepoint-transcript-search" / "scripts" / "gp_search.py"
    )

    # 1. External host-direct data_gateway.py -> rejected immediately
    ext_dg = subprocess.run(
        [sys.executable, dg_script, "fetch", "guidepoint", "--operation=search_library", "--query=cloud", "--limit=1", "--timeout=5"],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ext_dg.returncode == 1
    ext_dg_json = json.loads(ext_dg.stdout)
    assert ext_dg_json["error_code"] == "maintenance_host_direct_paused"

    # 2. External host-direct gp_search.py -> rejected immediately
    ext_gp = subprocess.run(
        [sys.executable, gp_script, "search", "--query", "cloud", "--size", "1", "--json", "--timeout", "5"],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ext_gp.returncode == 1
    ext_gp_json = json.loads(ext_gp.stdout)
    assert ext_gp_json["error_code"] == "maintenance_host_direct_paused"

    # 3. Internal bridge call (DALTON_BRIDGE_INTERNAL_CALL=1 via researchdata_mcp.py -> data_gateway.py) -> SUCCEEDS
    internal_env = {**base_env, "DALTON_BRIDGE_INTERNAL_CALL": "1"}
    rpc_in = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "research",
                "arguments": {
                    "route": "guidepoint",
                    "operation": "search_library",
                    "query": "cloud",
                    "limit": 1,
                    "timeout": 5,
                },
            },
        }
    ) + "\n"
    int_mcp = subprocess.run(
        [sys.executable, mcp_script],
        input=rpc_in,
        env=internal_env,
        capture_output=True,
        text=True,
        check=True,
    )
    int_reply = json.loads(int_mcp.stdout.strip().splitlines()[-1])
    assert int_reply["result"]["isError"] is False
    int_payload = json.loads(int_reply["result"]["content"][0]["text"])
    assert int_payload["ok"] is True

    # 4. Controlled ops probe (DALTON_OPS_CONTROLLED_PROBE=1) -> BOTH data_gateway.py & gp_search.py SUCCEED
    probe_env = {**base_env, "DALTON_OPS_CONTROLLED_PROBE": "1"}
    ok_dg = subprocess.run(
        [sys.executable, dg_script, "fetch", "guidepoint", "--operation=search_library", "--query=cloud", "--limit=1", "--timeout=5"],
        env=probe_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(ok_dg.stdout)["ok"] is True

    ok_gp = subprocess.run(
        [sys.executable, gp_script, "search", "--query", "cloud", "--size", "1", "--json", "--timeout", "5"],
        env=probe_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert ok_gp.returncode == 0
    assert json.loads(ok_gp.stdout)["data"][0]["transcript_id"] == "gp-1"


def test_step2_real_package_release_and_core_release_gate(tmp_path: Path) -> None:
    """Run real `scripts/standalone/package_release.sh` and assert all 3 gates in out-of-package checker."""
    checker = _load_script_module(
        "_test_b3b_pkg_checker", SCRIPTS_DALTON_DIR / "ops" / "check_core_release_package.py"
    )
    export_dir = tmp_path / "core_export"
    pkg_res = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "standalone" / "package_release.sh"),
            "--repo",
            str(REPO_ROOT),
            "--ref",
            "HEAD",
            "--output",
            str(export_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert pkg_res.returncode == 0, f"package_release.sh failed:\n{pkg_res.stderr}"

    # Overlay working-tree edits of migrated public/standalone targets onto export_dir so this gate
    # verifies the exact working tree both before and after committing (preserving DENY_PATHS absence).
    for rel in checker.EXPORT_MIGRATED_TARGETS:
        src_p = REPO_ROOT / rel
        dst_p = export_dir / rel
        if not src_p.exists():
            continue
        if src_p.is_file():
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_p, dst_p)
        else:
            if dst_p.exists():
                shutil.rmtree(dst_p)
            shutil.copytree(
                src_p,
                dst_p,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "package_release.sh"),
            )

    # Confirm physical absence of package_release.sh & private paths in the real export
    assert not (export_dir / "scripts" / "standalone" / "package_release.sh").exists()
    assert not (export_dir / "scripts" / "dalton" / "ops").exists()
    assert not (export_dir / "scripts" / "dalton" / "research_runtime").exists()
    assert not (export_dir / "scripts" / "dalton" / "researchdata_bridge.py").exists()
    assert not (export_dir / "plugins" / "dalton_data").exists()

    # Run out-of-package checker CLI and assert exit 0 + zero hits across all 3 gates
    cli_res = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DALTON_DIR / "ops" / "check_core_release_package.py"),
            "--repo-root",
            str(REPO_ROOT),
            "--export-dir",
            str(export_dir),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(cli_res.stdout)
    assert cli_res.returncode == 0, f"Core release gate failed:\n{json.dumps(report, indent=2)}"
    assert report["ok"] is True
    assert report["public_source_hits"] == []
    assert report["forbidden_paths_present"] == []
    assert report["export_migrated_hits"] == []
    assert report["whole_export_commercial_hits"] == []
