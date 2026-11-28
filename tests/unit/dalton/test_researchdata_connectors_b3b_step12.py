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
    #    Test REAL backend capability helper, REAL research-host HTTP app, and REAL legacy bridge requests
    backend_env = load_deployment_capability_env(repo_env_file=repo_env, environ={})
    host_loaded_env: dict[str, str] = {}
    load_shared_and_host_env(env_file=str(host_env), repo_env_file=str(repo_env), environ=host_loaded_env)

    monkeypatch.setenv("DALTON_DOTENV_PATH", str(repo_env))
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))
    pre_cfg = bridge_mod.BridgeConfig()
    pre_bridge = bridge_mod.ResearchDataBridge(pre_cfg)

    caps_backend = effective_capabilities(environ=backend_env)
    caps_host = effective_capabilities(environ=host_loaded_env)
    caps_bridge = effective_capabilities(environ=pre_cfg.capability_env)
    assert caps_backend == caps_host == caps_bridge
    assert CapabilityId.SALES_NOTES not in caps_backend
    assert is_batch3_connectors_enabled(environ=backend_env) is False
    assert is_batch3_connectors_enabled(environ=host_loaded_env) is False
    assert is_batch3_connectors_enabled(environ=pre_cfg.capability_env) is False
    assert is_bridge_route_migrated_to_connector("guidepoint", environ=pre_cfg.capability_env) is False
    assert is_bridge_ingest_migrated_to_connector("wiki", environ=pre_cfg.capability_env) is False

    # Real legacy bridge rejects capability-disabled route on both /mcp and /sales-notes/ingest
    sn_mcp_resp = await pre_bridge.handle_tools_call(
        1,
        {"name": "research", "arguments": {"route": "sales-notes", "query": "NVDA"}},
    )
    assert sn_mcp_resp["result"]["isError"] is True
    sn_mcp_body = json.loads(sn_mcp_resp["result"]["content"][0]["text"])
    assert sn_mcp_body["error_code"] == "capability_disabled"
    assert sn_mcp_body["http_status"] == 403

    sn_ing_code, sn_ing_body = await pre_bridge.handle_sales_notes_ingest(
        {"institution": "MS", "title": "Note", "content": "hi"}
    )
    assert sn_ing_code == 403 and sn_ing_body["error_code"] == "capability_disabled"

    # Real research-host rejects /mcp/sales-notes and /ingest/sales-notes with 403 capability_disabled
    pre_rt = ResearchHostRuntime(
        ResearchHostConfig(
            data_root=tmp_path,
            mcp_token="rh-tok-1",
            admin_token="rh-adm-1",
            profile=DeploymentProfile.EVERFLOW,
            extra_modules=["host_connectors"],
            extra_python_paths=[str(RESEARCH_RUNTIME_DIR)],
            environ=host_loaded_env,
        ),
        capabilities_provider=lambda: effective_capabilities(environ=host_loaded_env),
    )
    pre_app = create_app(runtime=pre_rt)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pre_app), base_url="http://127.0.0.1:8951"
    ) as rh_client:
        r_sn = await rh_client.post(
            "/mcp/sales-notes",
            headers={"Authorization": "Bearer rh-tok-1"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert r_sn.status_code == 403
        assert r_sn.json()["error_code"] == "capability_disabled"
        r_sn_ing = await rh_client.post(
            "/ingest/sales-notes",
            headers={"Authorization": "Bearer rh-adm-1"},
            json={"institution": "MS", "title": "Note", "content": "hi"},
        )
        assert r_sn_ing.status_code == 403
        assert r_sn_ing.json()["error_code"] == "capability_disabled"
    await pre_rt.shutdown()

    # 3. Post-cutover: adding DALTON_RESEARCH_HOST_URL to shared repo .env flips ALL 3 ends & closes bridge routes
    repo_env.write_text(
        repo_env.read_text(encoding="utf-8") + "DALTON_RESEARCH_HOST_URL=http://host.docker.internal:8951\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DALTON_DOTENV_PATH", str(repo_env))
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

    def _http_unreachable(
        method: str,
        url: str,
        *,
        headers: Any,
        body: bytes | None,
        timeout_s: float,
    ) -> tuple[int | None, dict[str, Any] | None, str]:
        _ = (method, url, headers, body, timeout_s)
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

    def _http_paused(
        method: str,
        url: str,
        *,
        headers: Any,
        body: bytes | None,
        timeout_s: float,
    ) -> tuple[int | None, dict[str, Any] | None, str]:
        _ = (method, url, headers, body, timeout_s)
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

        def _fake_proxy_urlopen(
            url: Any,
            data: Any = None,
            timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
            *,
            context: Any = None,
        ) -> Any:
            _ = (data, context)
            seen_proxy_upstream["timeout"] = timeout
            seen_proxy_upstream["body"] = json.loads((url.data or b"{}").decode("utf-8"))
            raise socket.timeout("timed out")

        monkeypatch.setattr(
            proxy_mod.urllib.request,
            "urlopen",
            create_autospec(proxy_mod.urllib.request.urlopen, side_effect=_fake_proxy_urlopen),
        )

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

        def _build_slow_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(_slow_transport_handler)
            return real_async_client(*args, **kwargs)

        monkeypatch.setattr(
            gp_conn_mod.httpx,
            "AsyncClient",
            create_autospec(real_async_client, side_effect=_build_slow_async_client),
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


def _snapshot_working_tree_repo(tmp_path: Path) -> Path:
    """Return REPO_ROOT if clean, else a temp git repo snapshotting HEAD + working-tree changes."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if not status.strip():
        return REPO_ROOT

    snap = tmp_path / "working-tree-repo"
    snap.mkdir()
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
    )
    try:
        subprocess.run(["tar", "-xf", "-", "-C", str(snap)], stdin=archive.stdout, check=True)
    finally:
        if archive.stdout is not None:
            archive.stdout.close()
        assert archive.wait() == 0

    for line in status.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        rel = line[3:].strip().strip('"')
        if " -> " in rel:
            old_rel, rel = rel.split(" -> ", 1)
            old_dst = snap / old_rel
            if old_dst.exists() or old_dst.is_symlink():
                old_dst.unlink()
        src = REPO_ROOT / rel
        dst = snap / rel
        if "D" in xy and not src.exists():
            if dst.exists() or dst.is_symlink():
                dst.unlink()
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    subprocess.run(["git", "init", "-q"], cwd=str(snap), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(snap), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(snap), check=True)
    subprocess.run(["git", "add", "-A", "-f"], cwd=str(snap), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "working-tree-snapshot"], cwd=str(snap), check=True)
    return snap


def test_step2_real_package_release_and_core_release_gate(tmp_path: Path) -> None:
    """Run real `scripts/standalone/package_release.sh` and assert all gates on the unmodified git archive export."""
    source_repo = _snapshot_working_tree_repo(tmp_path)
    export_dir = tmp_path / "core_export"
    pkg_res = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "standalone" / "package_release.sh"),
            "--repo",
            str(source_repo),
            "--ref",
            "HEAD",
            "--output",
            str(export_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert pkg_res.returncode == 0, f"package_release.sh failed:\n{pkg_res.stdout}\n{pkg_res.stderr}"

    # Confirm physical absence of package_release.sh & private paths in the unmodified real export
    assert not (export_dir / "scripts" / "standalone" / "package_release.sh").exists()
    assert not (export_dir / "scripts" / "dalton" / "ops").exists()
    assert not (export_dir / "scripts" / "dalton" / "research_runtime").exists()
    assert not (export_dir / "scripts" / "dalton" / "researchdata_bridge.py").exists()
    assert not (export_dir / "plugins" / "dalton_data").exists()

    # Run out-of-package checker CLI directly on the unmodified export and assert exit 0 + zero hits
    cli_res = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DALTON_DIR / "ops" / "check_core_release_package.py"),
            "--repo-root",
            str(source_repo),
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
    assert report["whole_export_forbidden_regex_hits"] == []


# ===========================================================================
# R2 REVIEW & REGRESSION FIX TESTS (ITEMS 1-13)
# ===========================================================================


@pytest.mark.asyncio
async def test_r2_item1_bridge_does_not_leak_shared_dotenv_secrets_to_env_or_subprocesses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 1: shared .env secrets stay out of os.environ, host-task env, and _run_stdio_rpc env; capability_env reads capability keys."""
    _clean_cap_env(monkeypatch)
    for k in ("SUPABASE_SECRET_KEY", "REDIS_PASSWORD", "DALTON_RESEARCH_ADMIN_TOKEN"):
        monkeypatch.delenv(k, raising=False)

    benv = _load_script_module(
        "_test_r2_benv_item1", SCRIPTS_DALTON_DIR / "bridge_subprocess_env.py"
    )
    bridge_mod = _load_script_module(
        "_test_r2_bridge_item1", SCRIPTS_DALTON_DIR / "researchdata_bridge.py"
    )

    shared_env = tmp_path / "shared.env"
    shared_env.write_text(
        "\n".join(
            [
                "APP_ENV=dalton",
                "DALTON_CAP_SALES_NOTES=false",
                "SUPABASE_SECRET_KEY=fake-supa-secret-123",
                "REDIS_PASSWORD=fake-redis-pass-456",
                "DALTON_RESEARCH_ADMIN_TOKEN=fake-admin-tok-789",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    alt_env = tmp_path / "alt.env"
    alt_env.write_text("DALTON_CAP_SALES_NOTES=true\n", encoding="utf-8")

    bridge_env = tmp_path / "researchdata-bridge.env"
    bridge_env.write_text("DALTON_BRIDGE_TOKEN=br-tok-r2\n", encoding="utf-8")

    # Verify DALTON_DOTENV_PATH takes precedence over DALTON_REPO_ENV_FILE (matching resolve_shared_dotenv_path)
    monkeypatch.setenv("DALTON_DOTENV_PATH", str(shared_env))
    monkeypatch.setenv("DALTON_REPO_ENV_FILE", str(alt_env))
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))

    cfg = bridge_mod.BridgeConfig()
    assert "SUPABASE_SECRET_KEY" not in os.environ
    assert "REDIS_PASSWORD" not in os.environ
    assert "DALTON_RESEARCH_ADMIN_TOKEN" not in os.environ
    assert cfg.capability_env.get("DALTON_CAP_SALES_NOTES") == "false"

    # Host task subprocess env strips secrets and DALTON_RESEARCH_ADMIN_TOKEN (even if present in os.environ)
    host_env = benv.build_host_task_subprocess_env(
        {**os.environ, "DALTON_RESEARCH_ADMIN_TOKEN": "fake-admin-tok-789"}
    )
    assert "SUPABASE_SECRET_KEY" not in host_env
    assert "REDIS_PASSWORD" not in host_env
    assert "DALTON_RESEARCH_ADMIN_TOKEN" not in host_env
    assert "DALTON_RESEARCH_ADMIN_TOKEN" in benv.BRIDGE_TOKEN_ENV_KEYS

    # Actual env passed to asyncio.create_subprocess_exec by _run_stdio_rpc also excludes all three keys
    monkeypatch.setenv("DALTON_RESEARCH_ADMIN_TOKEN", "fake-admin-tok-789")
    bridge = bridge_mod.ResearchDataBridge(cfg)
    captured_envs: list[dict[str, str]] = []
    monkeypatch.setattr(
        bridge_mod,
        "_kill_process_group",
        create_autospec(bridge_mod._kill_process_group),
    )

    async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        _ = cmd
        captured_envs.append(dict(kwargs.get("env") or {}))
        proc = create_autospec(asyncio.subprocess.Process, instance=True)
        proc.pid = 425101
        proc.returncode = 0
        proc.communicate.return_value = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode("utf-8") + b"\n",
            b"",
        )
        return proc

    monkeypatch.setattr(
        bridge_mod.asyncio,
        "create_subprocess_exec",
        create_autospec(asyncio.create_subprocess_exec, side_effect=_fake_exec),
    )
    await bridge._run_stdio_rpc([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], 5.0)
    assert captured_envs
    for secret_key in ("SUPABASE_SECRET_KEY", "REDIS_PASSWORD", "DALTON_RESEARCH_ADMIN_TOKEN"):
        assert secret_key not in captured_envs[0]


def _pid_is_gone_or_zombie(pid: int) -> bool:
    try:
        res = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        stat_str = res.stdout.strip()
        return res.returncode != 0 or not stat_str or stat_str.startswith("Z")
    except PermissionError:
        # macOS Seatbelt sandbox blocks exec of setuid /bin/ps; fall back to signal 0 probe.
        try:
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            return True


@pytest.mark.asyncio
async def test_r2_item2_bridge_internal_data_gateway_timeout_reaps_grandchild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 2: when data_gateway times out BEFORE the bridge and mcp exits normally, _run_stdio_rpc finally kills the process group so bash grandchildren do not survive as orphans."""
    _clean_cap_env(monkeypatch)
    bridge_mod = _load_script_module(
        "_test_r2_bridge_item2", SCRIPTS_DALTON_DIR / "researchdata_bridge.py"
    )

    # Build a minimal research_runtime copy with a bash skill that spawns a background sleep grandchild
    rt_code = tmp_path / "rt_code"
    (rt_code / "scripts").mkdir(parents=True)
    (rt_code / "config").mkdir(parents=True)
    (rt_code / "skills" / "findata-analyst").mkdir(parents=True)
    shutil.copy2(
        RESEARCH_RUNTIME_DIR / "scripts" / "researchdata_mcp.py",
        rt_code / "scripts" / "researchdata_mcp.py",
    )
    shutil.copy2(
        RESEARCH_RUNTIME_DIR / "scripts" / "data_gateway.py",
        rt_code / "scripts" / "data_gateway.py",
    )
    shutil.copy2(
        RESEARCH_RUNTIME_DIR / "config" / "data_sources.json",
        rt_code / "config" / "data_sources.json",
    )

    grand_pid_file = tmp_path / "grandchild.pid"
    run_sh = rt_code / "skills" / "findata-analyst" / "run.sh"
    run_sh.write_text(
        f"""#!/usr/bin/env bash
sleep 30 &
echo "$!" > {str(grand_pid_file)!r}
sleep 30
""",
        encoding="utf-8",
    )
    run_sh.chmod(0o755)

    bridge_env = tmp_path / "bridge.env"
    bridge_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))
    monkeypatch.setenv("DALTON_RESEARCH_CODE", str(rt_code))
    monkeypatch.setenv("DALTON_RESEARCH_HOME", str(tmp_path / "home"))
    cfg = bridge_mod.BridgeConfig()
    cfg.python_bin = sys.executable
    cfg.mcp_script = (rt_code / "scripts" / "researchdata_mcp.py").resolve()
    bridge = bridge_mod.ResearchDataBridge(cfg)

    # Inner request timeout=1s while bridge outer timeout=10s -> data_gateway times out first and returns JSON,
    # researchdata_mcp.py exits 0 normally, and _run_stdio_rpc finally kills the process group!
    resps = await bridge._run_stdio_rpc(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "finance",
                    "arguments": {
                        "route": "sec",
                        "operation": "list-filings",
                        "ticker": "AAPL",
                        "timeout": 1,
                    },
                },
            }
        ],
        timeout_s=10.0,
    )
    assert resps
    payload = json.loads(resps[0]["result"]["content"][0]["text"])
    assert payload["status"] == "timeout"

    assert grand_pid_file.is_file(), "bash grandchild PID was never recorded"
    grand_pid = int(grand_pid_file.read_text(encoding="utf-8").strip())
    await asyncio.sleep(0.2)
    assert _pid_is_gone_or_zombie(grand_pid), f"Grandchild pid={grand_pid} survived as orphan!"


@pytest.mark.asyncio
async def test_r2_item3_route_all_shared_deadline_cancels_all_three_legs_and_zeros_inflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 3: route=all timeout=3 with 3 slow upstreams completes <= 3.5s, cancels all 3 legs, and returns inflight to 0."""
    _clean_cap_env(monkeypatch)
    bridge_mod = _load_script_module(
        "_test_r2_bridge_item3", SCRIPTS_DALTON_DIR / "researchdata_bridge.py"
    )
    bridge_env = tmp_path / "bridge.env"
    bridge_env.write_text("", encoding="utf-8")
    repo_env = tmp_path / "repo.env"
    repo_env.write_text("APP_ENV=dalton\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_DOTENV_PATH", str(repo_env))
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_BRIDGE_MAX_INFLIGHT", "8")
    monkeypatch.setenv("DALTON_BRIDGE_MAX_INFLIGHT_UNSIGNED", "8")

    cfg = bridge_mod.BridgeConfig()
    bridge = bridge_mod.ResearchDataBridge(cfg)
    assert bridge._compute_fanout_timeout({"timeout": 3}, 60) == 3

    cancelled_routes: list[str] = []
    started_routes: list[str] = []

    async def _slow_rpc(
        requests: list[dict[str, Any]],
        timeout_s: float,
    ) -> list[dict[str, Any]]:
        route = str(requests[0]["params"]["arguments"]["route"])
        started_routes.append(route)
        try:
            await asyncio.sleep(max(20.0, timeout_s + 10.0))
        except asyncio.CancelledError:
            cancelled_routes.append(route)
            raise
        return []

    monkeypatch.setattr(
        bridge,
        "_run_stdio_rpc",
        create_autospec(bridge._run_stdio_rpc, side_effect=_slow_rpc),
    )

    t0 = time.monotonic()
    resp = await bridge.handle_tools_call(
        "all-deadline-3s",
        {
            "name": "research",
            "arguments": {
                "route": "all",
                "operation": "search",
                "query": "CATL",
                "timeout": 3,
            },
        },
    )
    elapsed = time.monotonic() - t0
    assert elapsed <= 3.5, f"route=all timeout=3 exceeded 3.5s budget: {elapsed:.2f}s"
    assert resp["result"]["isError"] is True
    assert set(started_routes) == {"all", "alphaengine", "guidepoint"}
    assert set(cancelled_routes) == {"all", "alphaengine", "guidepoint"}
    assert bridge.research_drain_status()["inflight"] == 0


@pytest.mark.enable_socket
def test_r2_item4_proxy_8943_relay_enforces_total_deadline_on_slow_chunked_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4 / r3 Item 2: real HTTP chunked server (0.5s x 20 chunks ~10s) with timeout=2 finishes on time and zeros _in_flight."""
    import http.client
    import http.server
    import threading

    _clean_cap_env(monkeypatch)
    gp_lock_mod = _load_script_module(
        "_test_r2_gp_lock_item4", RESEARCH_RUNTIME_DIR / "guidepoint_lock.py"
    )
    proxy_mod = _load_script_module(
        "_test_r2_gp_proxy_item4", RESEARCH_RUNTIME_DIR / "scripts" / "guidepoint_mcp_proxy.py"
    )

    journal_file = tmp_path / "handoff-journal.json"
    token_file = tmp_path / "config" / "guidepoint_mcp_tokens.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(
        json.dumps(
            {
                "access_token": "tok-chunk",
                "refresh_token": "ref-chunk",
                "obtained_at": time.time(),
                "expires_in": 600,
            }
        ),
        encoding="utf-8",
    )
    gp_lock_mod.set_target_owner(gp_lock_mod.OWNER_LEGACY_PROXY, ownership_file=journal_file)
    monkeypatch.setenv("DALTON_GUIDEPOINT_OWNERSHIP_FILE", str(journal_file))
    monkeypatch.setenv("GUIDEPOINT_TOKENS_FILE", str(token_file))

    chunks_sent = 0
    upstream_stop = threading.Event()
    upstream_done = threading.Event()

    req_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search_library",
                "arguments": {"query": "cloud", "size": 1, "timeout": 2},
            },
        }
    ).encode("utf-8")

    # Probe whether loopback AF_INET connect is permitted (blocked in macOS sandbox, allowed outside)
    loopback_tcp_ok = False
    probe_srv: http.server.HTTPServer | None = None
    try:
        class _ProbeHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, _fmt: str, *_args: Any) -> None:
                pass

        probe_srv = http.server.HTTPServer(("127.0.0.1", 0), _ProbeHandler)
        pt = threading.Thread(target=probe_srv.handle_request, daemon=True)
        pt.start()
        with proxy_mod.urllib.request.urlopen(
            f"http://127.0.0.1:{probe_srv.server_address[1]}/", timeout=1.0
        ) as pr:
            loopback_tcp_ok = pr.status == 200
        pt.join(timeout=1.0)
    except Exception:
        loopback_tcp_ok = False
    finally:
        if probe_srv is not None:
            probe_srv.server_close()

    proxy_mod.initialize_runtime(ownership_file=journal_file, token_store=token_file)
    try:
        if loopback_tcp_ok:
            class _SlowUpstreamHandler(http.server.BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def do_POST(self) -> None:
                    nonlocal chunks_sent
                    clen = int(self.headers.get("Content-Length", 0) or 0)
                    if clen:
                        self.rfile.read(clen)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    try:
                        for idx in range(20):
                            if upstream_stop.wait(0.5):
                                break
                            chunk = f"data: chunk-{idx}\n\n".encode("utf-8")
                            frame = f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n"
                            self.wfile.write(frame)
                            self.wfile.flush()
                            chunks_sent += 1
                        else:
                            self.wfile.write(b"0\r\n\r\n")
                            self.wfile.flush()
                    except OSError:
                        pass
                    finally:
                        upstream_done.set()

                def log_message(self, _fmt: str, *_args: Any) -> None:
                    pass

            up_srv = proxy_mod.ThreadingHTTPServer(("127.0.0.1", 0), _SlowUpstreamHandler)
            up_thread = threading.Thread(target=up_srv.serve_forever, daemon=True)
            up_thread.start()
            monkeypatch.setattr(proxy_mod, "UPSTREAM", f"http://127.0.0.1:{up_srv.server_address[1]}/mcp")
            px_srv = proxy_mod.ThreadingHTTPServer(("127.0.0.1", 0), proxy_mod.ProxyHandler)
            px_thread = threading.Thread(target=px_srv.serve_forever, daemon=True)
            px_thread.start()
            try:
                http_req = proxy_mod.urllib.request.Request(
                    f"http://127.0.0.1:{px_srv.server_address[1]}/mcp",
                    data=req_body,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                t0 = time.monotonic()
                with proxy_mod.urllib.request.urlopen(http_req, timeout=5.0) as client_resp:
                    try:
                        received_bytes = client_resp.read()
                    except http.client.IncompleteRead as exc:
                        received_bytes = exc.partial
                elapsed = time.monotonic() - t0
                assert elapsed < 3.2, f"Slow chunked relay exceeded total deadline: {elapsed:.2f}s"
                assert chunks_sent < 10
                assert b"data: chunk-0" in received_bytes
                assert proxy_mod.health_payload()["in_flight"] == 0
            finally:
                upstream_stop.set()
                px_srv.shutdown()
                px_srv.server_close()
                up_srv.shutdown()
                up_srv.server_close()
                px_thread.join(timeout=2.0)
                up_thread.join(timeout=2.0)
        else:
            def _start_slow_chunked_http_response(timeout_s: float) -> http.client.HTTPResponse:
                up_cli, up_srv = socket.socketpair()
                up_cli.settimeout(timeout_s)

                def _serve_chunks() -> None:
                    nonlocal chunks_sent
                    try:
                        up_srv.sendall(
                            b"HTTP/1.1 200 OK\r\n"
                            b"Content-Type: text/event-stream\r\n"
                            b"Transfer-Encoding: chunked\r\n\r\n"
                        )
                        for idx in range(20):
                            if upstream_stop.wait(0.5):
                                break
                            chunk = f"data: chunk-{idx}\n\n".encode("utf-8")
                            frame = f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n"
                            up_srv.sendall(frame)
                            chunks_sent += 1
                        else:
                            up_srv.sendall(b"0\r\n\r\n")
                    except OSError:
                        pass
                    finally:
                        try:
                            up_srv.close()
                        except OSError:
                            pass
                        upstream_done.set()

                t = threading.Thread(target=_serve_chunks, daemon=True)
                t.start()
                resp = http.client.HTTPResponse(up_cli)
                resp.begin()
                return resp

            def _fake_urlopen(
                url: Any,
                data: Any = None,
                timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
                *,
                context: Any = None,
            ) -> Any:
                _ = (url, data, context)
                eff_t = float(timeout) if isinstance(timeout, (int, float)) else 2.0
                return _start_slow_chunked_http_response(eff_t)

            monkeypatch.setattr(
                proxy_mod.urllib.request,
                "urlopen",
                create_autospec(proxy_mod.urllib.request.urlopen, side_effect=_fake_urlopen),
            )
            raw_http_req = (
                b"POST /mcp HTTP/1.1\r\n"
                b"Host: 127.0.0.1:8943\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(req_body)}\r\n\r\n".encode("ascii")
                + req_body
            )
            cli_sock, srv_sock = socket.socketpair()
            cli_sock.settimeout(5.0)
            cli_sock.sendall(raw_http_req)
            cli_sock.shutdown(socket.SHUT_WR)

            received = bytearray()

            def _drain_client() -> None:
                try:
                    while True:
                        part = cli_sock.recv(4096)
                        if not part:
                            break
                        received.extend(part)
                except OSError:
                    pass
                finally:
                    cli_sock.close()

            reader_t = threading.Thread(target=_drain_client, daemon=True)
            reader_t.start()

            t0 = time.monotonic()
            try:
                proxy_mod.ProxyHandler(srv_sock, ("127.0.0.1", 0), None)
            finally:
                try:
                    srv_sock.close()
                except OSError:
                    pass
            elapsed = time.monotonic() - t0
            reader_t.join(timeout=2.0)
            upstream_stop.set()
            upstream_done.wait(timeout=2.0)

            assert elapsed < 3.2, f"Slow chunked relay exceeded total deadline: {elapsed:.2f}s"
            assert chunks_sent < 10, f"Relay kept reading past 2s deadline ({chunks_sent} chunks)"
            assert received.startswith(b"HTTP/1.1 200")
            assert b"data: chunk-0" in bytes(received)
            assert proxy_mod.health_payload()["in_flight"] == 0
    finally:
        upstream_stop.set()
        proxy_mod.release_owner_lock()


def test_r2_item5_bridge_everflow_prod_all_mapped_routes_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 5 regression guard: under Everflow prod env (APP_ENV=dalton, no DALTON_CAP_*, no DALTON_RESEARCH_HOST_URL), every BRIDGE_ROUTE_CAPABILITY_MAP key is enabled."""
    from src.dalton.capabilities import BRIDGE_ROUTE_CAPABILITY_MAP, is_research_source_enabled

    _clean_cap_env(monkeypatch)
    bridge_mod = _load_script_module(
        "_test_r2_bridge_item5", SCRIPTS_DALTON_DIR / "researchdata_bridge.py"
    )
    repo_env = tmp_path / "everflow.env"
    repo_env.write_text("APP_ENV=dalton\n", encoding="utf-8")
    bridge_env = tmp_path / "bridge.env"
    bridge_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_DOTENV_PATH", str(repo_env))
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))

    cfg = bridge_mod.BridgeConfig()
    bridge = bridge_mod.ResearchDataBridge(cfg)
    active_caps = bridge._effective_capabilities_cached()
    for route_id in sorted(BRIDGE_ROUTE_CAPABILITY_MAP):
        assert is_research_source_enabled(route_id, capabilities=active_caps) is True, (
            f"Everflow prod route {route_id!r} unexpectedly disabled!"
        )
        assert bridge._is_route_capability_disabled(route_id) is False, (
            f"Bridge unexpectedly disabled Everflow prod route {route_id!r}!"
        )
    # Routes not in BRIDGE_ROUTE_CAPABILITY_MAP remain allowed through
    assert bridge._is_route_capability_disabled("sec") is False
    assert bridge._is_route_capability_disabled("fred") is False


@pytest.mark.asyncio
async def test_r2_item6_maintenance_pause_defers_command_and_wakeup_outbox_without_burning_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 6: 6 consecutive paused executions of command_report_back & wakeup defer via reschedule_claimed_job without burning attempts, and succeed once resumed."""
    from src.dalton.maintenance_gate import enforce_maintenance_admission_for_wakeup
    from src.server.database.runs import outbox as outbox_db
    from src.server.services import hook_outbox
    from src.server.services.report_back import command as cmd_rb

    state_file = tmp_path / "maintenance-admission.json"
    monkeypatch.setenv("DALTON_MAINTENANCE_ADMISSION_FILE", str(state_file))
    write_maintenance_state_atomic(
        {
            "version": 1,
            "revision": 1,
            "paused": True,
            "pause_host_direct": True,
            "allow_workspace_ids": [],
            "reason": "maintenance_window",
            "operation": "switch",
            "rehearsal_keep_paused_until": None,
            "updated_at": "2026-11-26T00:00:00+00:00",
        },
        state_file=state_file,
    )

    reschedule_mock = create_autospec(outbox_db.reschedule_claimed_job, return_value="pending")
    nack_mock = create_autospec(outbox_db.nack_outbox_job, return_value="dead")
    ack_mock = create_autospec(outbox_db.ack_outbox_job, return_value=True)
    monkeypatch.setattr(outbox_db, "reschedule_claimed_job", reschedule_mock)
    monkeypatch.setattr(outbox_db, "nack_outbox_job", nack_mock)
    monkeypatch.setattr(outbox_db, "ack_outbox_job", ack_mock)

    delivered: list[str] = []
    riders_released: list[int] = []

    async def _fake_run_report_back(job: dict[str, Any], _pipeline: Any) -> None:
        delivered.append(f"cmd:{job['hook_outbox_id']}")

    monkeypatch.setattr(
        cmd_rb,
        "run_report_back",
        create_autospec(cmd_rb.run_report_back, side_effect=_fake_run_report_back),
    )

    async def _wakeup_exec(job: dict[str, Any]) -> None:
        async def _release() -> None:
            riders_released.append(1)

        await enforce_maintenance_admission_for_wakeup("ws-wake", release_riders=_release)
        delivered.append(f"wake:{job['hook_outbox_id']}")

    monkeypatch.setitem(hook_outbox._EXECUTORS, "command_report_back", cmd_rb.execute_command_report_back)
    monkeypatch.setitem(hook_outbox._EXECUTORS, "wakeup", _wakeup_exec)

    drainer = hook_outbox.HookOutboxDrainer()
    cmd_job = {
        "hook_outbox_id": "job-cmd-1",
        "hook_type": "command_report_back",
        "attempts": 1,
        "conversation_thread_id": "thr-cmd-1",
    }
    wake_job = {
        "hook_outbox_id": "job-wake-1",
        "hook_type": "wakeup",
        "attempts": 1,
        "conversation_thread_id": "thr-wake-1",
    }

    # 6 consecutive runs during pause -> rescheduled every time, 0 nacks, attempts stay at 1
    for _ in range(6):
        await drainer._execute(cmd_job)
        await drainer._execute(wake_job)

    assert reschedule_mock.await_count == 12
    assert nack_mock.await_count == 0
    assert ack_mock.await_count == 0
    assert delivered == []
    assert len(riders_released) == 6
    for call in reschedule_mock.await_args_list:
        assert call.kwargs["attempts"] == 1
        assert call.kwargs["delay_seconds"] == 60.0

    # Resume admission -> both deliver and ack normally
    write_maintenance_state_atomic(
        {
            "version": 1,
            "revision": 2,
            "paused": False,
            "pause_host_direct": False,
            "allow_workspace_ids": [],
            "reason": "resumed",
            "operation": "switch",
            "rehearsal_keep_paused_until": None,
            "updated_at": "2026-11-26T00:01:00+00:00",
        },
        state_file=state_file,
    )
    await drainer._execute(cmd_job)
    await drainer._execute(wake_job)
    assert delivered == ["cmd:job-cmd-1", "wake:job-wake-1"]
    assert ack_mock.await_count == 2
    assert nack_mock.await_count == 0


@pytest.mark.asyncio
async def test_r2_item7_manual_automation_trigger_allowlist_and_autospec_get_automation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 7: enforce_maintenance_admission_for_automation passes user_id to get_automation (verified with create_autospec)."""
    from src.dalton.maintenance_gate import enforce_maintenance_admission_for_automation
    from src.server.database import automation as auto_db

    state_file = tmp_path / "maintenance-admission.json"
    write_maintenance_state_atomic(
        {
            "version": 1,
            "revision": 1,
            "paused": True,
            "pause_host_direct": False,
            "allow_workspace_ids": ["ws-rehearsal-auto"],
            "reason": "rehearsal",
            "operation": "switch",
            "rehearsal_keep_paused_until": None,
            "updated_at": "2026-11-26T00:00:00+00:00",
        },
        state_file=state_file,
    )

    async def _get_auto(
        automation_id: str,
        user_id: str,
        *,
        computer_id: str | None = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        _ = (computer_id, conn)
        if automation_id == "auto-allowed" and user_id == "user-1":
            return {"automation_id": automation_id, "workspace_id": "ws-rehearsal-auto"}
        if automation_id == "auto-blocked" and user_id == "user-1":
            return {"automation_id": automation_id, "workspace_id": "ws-other"}
        return None

    get_auto_mock = create_autospec(auto_db.get_automation, side_effect=_get_auto)
    monkeypatch.setattr(auto_db, "get_automation", get_auto_mock)

    st = await enforce_maintenance_admission_for_automation(
        "auto-allowed", user_id="user-1", state_file=state_file
    )
    assert st.paused is True
    get_auto_mock.assert_awaited_once_with("auto-allowed", "user-1")

    with pytest.raises(MaintenanceAdmissionPausedError) as exc_info:
        await enforce_maintenance_admission_for_automation(
            "auto-blocked", user_id="user-1", state_file=state_file
        )
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_r2_item8_acquire_lock_releases_on_corrupt_tokens_and_does_not_block_healthz(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 8: corrupt token file returns 409 and releases newly acquired lock (standby=True); lock wait runs off event loop so /healthz responds < 1s."""
    _clean_cap_env(monkeypatch)
    gp_lock_mod = _load_script_module(
        "_test_r2_gp_lock_item8", RESEARCH_RUNTIME_DIR / "guidepoint_lock.py"
    )
    gp_conn_mod = _load_script_module(
        "_test_r2_gp_conn_item8", RESEARCH_RUNTIME_DIR / "host_connectors" / "guidepoint.py"
    )

    journal_file = tmp_path / "handoff-journal.json"
    token_file = tmp_path / "config" / "guidepoint_mcp_tokens.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("{corrupt-json", encoding="utf-8")
    gp_lock_mod.set_target_owner(gp_lock_mod.OWNER_LEGACY_PROXY, ownership_file=journal_file)

    tm = gp_conn_mod.GuidepointTokenManager(token_file, ownership_file=journal_file)
    assert tm.initialize_from_ownership(timeout_s=1.0) is False
    assert tm.standby is True

    rt = ResearchHostRuntime(
        ResearchHostConfig(
            data_root=tmp_path,
            mcp_token="mcp-i8",
            admin_token="adm-i8",
            profile=DeploymentProfile.EVERFLOW,
        ),
        capabilities_provider=lambda: frozenset({CapabilityId.GUIDEPOINT}),
    )
    rt.registry.register_lock_handoff(
        "guidepoint", tm.acquire_lock_for_handoff, capability=CapabilityId.GUIDEPOINT
    )
    rt.registry.register_health_probe("guidepoint", tm.health)
    app = create_app(runtime=rt)

    # 1. Switch target_owner -> research_host_8951 with corrupt token file -> 409, lock released, standby=True
    gp_lock_mod.set_target_owner(gp_lock_mod.OWNER_RESEARCH_HOST, ownership_file=journal_file)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8951"
    ) as client:
        r_bad = await client.post(
            "/admin/guidepoint/acquire-lock",
            headers={"Authorization": "Bearer adm-i8"},
            json={"timeout_s": 1.0},
        )
        assert r_bad.status_code == 409
        assert tm.lock_held is False
        assert tm.standby is True

        # Verify another holder can acquire the flock immediately (proves no leaked flock!)
        external_lock = gp_lock_mod.GuidepointOwnerLock(token_file=token_file)
        external_lock.acquire(timeout_s=0.5)
        try:
            # 2. While external_lock holds the flock and acquire-lock waits 2.0s in thread pool,
            #    /healthz MUST respond in < 1.0s (event loop not blocked!).
            acq_task = asyncio.create_task(
                client.post(
                    "/admin/guidepoint/acquire-lock",
                    headers={"Authorization": "Bearer adm-i8"},
                    json={"timeout_s": 2.0},
                )
            )
            await asyncio.sleep(0.15)
            t0 = time.monotonic()
            hz = await client.get("/healthz")
            hz_elapsed = time.monotonic() - t0
            assert hz.status_code == 200
            assert hz_elapsed < 1.0, f"/healthz blocked during acquire-lock wait ({hz_elapsed:.2f}s)"
            r_timeout = await acq_task
            assert r_timeout.status_code == 409
        finally:
            external_lock.release()
    await rt.shutdown()


def test_r2_item9_verify_container_maintenance_gate_default_240s_and_per_io_deadline() -> None:
    """Item 9: DEFAULT_CONTAINER_VERIFY_DEADLINE_S == 240s, and every I/O recomputes remaining time so total never exceeds deadline."""
    from src.dalton.maintenance_gate import DEFAULT_CONTAINER_VERIFY_DEADLINE_S

    assert DEFAULT_CONTAINER_VERIFY_DEADLINE_S == 240.0

    fake_now = [0.0]
    observed_timeouts: list[tuple[str, float]] = []

    def _clock() -> float:
        return fake_now[0]

    def _sleep(sec: float) -> None:
        fake_now[0] += sec

    def _slow_docker(args: list[str], timeout_s: float) -> tuple[int, str, str]:
        op = args[0]
        observed_timeouts.append((op, timeout_s))
        fake_now[0] += min(1.2, timeout_s)
        if op == "inspect":
            return 0, "true\n", ""
        return 0, json.dumps({"revision": 5, "paused": True}), ""

    def _slow_http(
        method: str,
        url: str,
        *,
        headers: Any,
        body: bytes | None,
        timeout_s: float,
    ) -> tuple[int | None, dict[str, Any] | None, str]:
        _ = (method, url, headers, body)
        observed_timeouts.append(("http", timeout_s))
        fake_now[0] += min(1.2, timeout_s)
        return None, None, "TimeoutError"

    res = verify_container_maintenance_gate(
        expected_revision=5,
        deadline_s=3.0,
        poll_interval_s=1.0,
        docker_runner=_slow_docker,
        http_requester=_slow_http,
        clock=_clock,
        sleep_fn=_sleep,
    )
    assert res.status == "unavailable"
    assert res.elapsed_s <= 3.0 + 1e-6
    assert observed_timeouts == [
        ("inspect", pytest.approx(3.0)),
        ("exec", pytest.approx(1.8)),
        ("http", pytest.approx(0.6)),
    ]


def test_r2_item10_package_release_fails_nonzero_on_proprietary_term_in_public_host(
    tmp_path: Path,
) -> None:
    """Item 10: a commit placing a proprietary term in src/dalton/research_host/ causes package_release.sh to exit non-zero."""
    fake_repo = tmp_path / "repo"
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(
        ["git", "clone", "--shared", "--no-tags", str(REPO_ROOT), str(fake_repo)],
        env=git_env,
        check=True,
        capture_output=True,
    )
    leak_file = fake_repo / "src" / "dalton" / "research_host" / "leak_test.py"
    leak_file.write_text('LEAK = "GuidepointTokenManager"\n', encoding="utf-8")
    subprocess.run(
        ["git", "add", "src/dalton/research_host/leak_test.py"],
        cwd=fake_repo,
        env=git_env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "leak commit"],
        cwd=fake_repo,
        env=git_env,
        check=True,
        capture_output=True,
    )

    out_dir = tmp_path / "should_not_exist"
    report_file = tmp_path / "scan_report.txt"
    pkg_res = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "standalone" / "package_release.sh"),
            "--repo",
            str(fake_repo),
            "--ref",
            "HEAD",
            "--output",
            str(out_dir),
            "--report",
            str(report_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert pkg_res.returncode != 0, "package_release.sh must fail non-zero when checker detects proprietary leak!"
    assert not out_dir.exists()
    assert report_file.is_file(), f"Report missing:\nstdout={pkg_res.stdout}\nstderr={pkg_res.stderr}"
    report_txt = report_file.read_text(encoding="utf-8")
    assert "FAIL" in report_txt
    assert "GuidepointTokenManager" in report_txt


@pytest.mark.asyncio
async def test_r2_item11_federated_sales_mail_alias_only_registered_in_everflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 11: sales_mail alias is present in Everflow (via host_connectors) and absent in Core."""
    _clean_cap_env(monkeypatch)

    # Core runtime (no extra host_connectors) -> tools/list has NO sales_mail
    core_rt = ResearchHostRuntime(
        ResearchHostConfig(
            data_root=tmp_path / "core",
            mcp_token="core-tok",
            admin_token="core-adm",
            profile=DeploymentProfile.CORE,
        ),
        capabilities_provider=lambda: frozenset({CapabilityId.COMPANY_WIKI}),
    )
    core_app = create_app(runtime=core_rt)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=core_app), base_url="http://127.0.0.1:8951"
    ) as client:
        r_core = await client.post(
            "/mcp/federated",
            headers={"Authorization": "Bearer core-tok"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert r_core.status_code == 200
        core_tools = {t["name"] for t in r_core.json()["result"]["tools"]}
        assert "sales_mail" not in core_tools
    await core_rt.shutdown()

    # Everflow runtime (with host_connectors) -> tools/list HAS sales_mail
    ef_rt = ResearchHostRuntime(
        ResearchHostConfig(
            data_root=tmp_path / "ef",
            mcp_token="ef-tok",
            admin_token="ef-adm",
            profile=DeploymentProfile.EVERFLOW,
            extra_modules=["host_connectors"],
            extra_python_paths=[str(RESEARCH_RUNTIME_DIR)],
        ),
        capabilities_provider=lambda: frozenset(
            {CapabilityId.COMPANY_WIKI, CapabilityId.SALES_NOTES, CapabilityId.TMTB}
        ),
    )
    ef_app = create_app(runtime=ef_rt)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ef_app), base_url="http://127.0.0.1:8951"
    ) as client:
        r_ef = await client.post(
            "/mcp/federated",
            headers={"Authorization": "Bearer ef-tok"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert r_ef.status_code == 200
        ef_tools = {t["name"] for t in r_ef.json()["result"]["tools"]}
        assert "sales_mail" in ef_tools
    await ef_rt.shutdown()


def test_r2_item12_host_direct_pause_fails_closed_on_corrupt_gate_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 12: corrupt or non-object maintenance-admission.json is treated as paused by both data_gateway.py and gp_search.py; missing file is unpaused."""
    _clean_cap_env(monkeypatch)
    dg_mod = _load_script_module(
        "_test_r2_dg_item12", RESEARCH_RUNTIME_DIR / "scripts" / "data_gateway.py"
    )
    gp_mod = _load_script_module(
        "_test_r2_gp_item12",
        RESEARCH_RUNTIME_DIR / "skills" / "guidepoint-transcript-search" / "scripts" / "gp_search.py",
    )

    gate_file = tmp_path / "maintenance-admission.json"
    bridge_env = tmp_path / "empty-bridge.env"
    bridge_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))
    monkeypatch.setenv("DALTON_MAINTENANCE_ADMISSION_FILE", str(gate_file))

    # Missing file -> not paused
    assert dg_mod._is_host_direct_paused() is False
    assert gp_mod._is_host_direct_paused() is False

    # Corrupt JSON -> fail closed (paused)
    gate_file.write_text("{not valid json", encoding="utf-8")
    assert dg_mod._is_host_direct_paused() is True
    assert gp_mod._is_host_direct_paused() is True

    # Non-object JSON -> fail closed (paused)
    gate_file.write_text("[1, 2, 3]", encoding="utf-8")
    assert dg_mod._is_host_direct_paused() is True
    assert gp_mod._is_host_direct_paused() is True


def test_r2_item13_maintenance_endpoints_return_503_when_service_token_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 13: /maintenance/admission and /maintenance/admission/probe fail closed with 503 maintenance_probe_unconfigured when INTERNAL_SERVICE_TOKEN is empty."""
    from src.config import env as server_env

    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    monkeypatch.setattr(server_env, "INTERNAL_SERVICE_TOKEN", "", raising=False)
    app = FastAPI()
    app.include_router(dalton_router)
    client = TestClient(app)

    for method, path in (
        ("GET", "/api/v1/dalton/maintenance/admission"),
        ("POST", "/api/v1/dalton/maintenance/admission/probe"),
    ):
        resp = client.request(method, path)
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["error_code"] == "maintenance_probe_unconfigured"


# ---------------------------------------------------------------------------
# r3 convergence tests (Codex r2 remaining 4 items)
# ---------------------------------------------------------------------------


async def _send_real_bridge_mcp_request(
    bridge_mod: Any,
    bridge: Any,
    *,
    token: str,
    rpc_body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Send a real HTTP/1.1 POST /mcp request over an OS socketpair into bridge_mod._handle_client."""
    cli_sock, srv_sock = socket.socketpair()
    cli_reader, cli_writer = await asyncio.open_connection(sock=cli_sock)
    srv_reader, srv_writer = await asyncio.open_connection(sock=srv_sock)
    server_task = asyncio.create_task(bridge_mod._handle_client(bridge, srv_reader, srv_writer))
    try:
        body_bytes = json.dumps(rpc_body, ensure_ascii=False).encode("utf-8")
        raw_req = (
            b"POST /mcp HTTP/1.1\r\n"
            b"Host: 127.0.0.1:8960\r\n"
            + f"Authorization: Bearer {token}\r\n".encode("ascii")
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body_bytes)}\r\n\r\n".encode("ascii")
            + body_bytes
        )
        cli_writer.write(raw_req)
        await cli_writer.drain()
        raw_resp = await asyncio.wait_for(cli_reader.read(), timeout=5.0)
    finally:
        cli_writer.close()
        await asyncio.gather(server_task, return_exceptions=True)

    header_blob, _, resp_body = raw_resp.partition(b"\r\n\r\n")
    status_line = header_blob.split(b"\r\n", 1)[0].decode("ascii")
    status_code = int(status_line.split(" ", 2)[1])
    return status_code, json.loads(resp_body.decode("utf-8"))


@pytest.mark.asyncio
async def test_r3_item1_bridge_mcp_http_403_and_federated_capability_pruning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r3 Item 1: real /mcp HTTP requests return 403 on disabled direct/explicit sources, and route=all / sales-mail prune disabled legs without invoking disabled connectors."""
    _clean_cap_env(monkeypatch)
    bridge_mod = _load_script_module(
        "_test_r3_bridge_item1", SCRIPTS_DALTON_DIR / "researchdata_bridge.py"
    )
    rl_mod = _load_script_module(
        "_test_r3_research_library_item1", RESEARCH_RUNTIME_DIR / "scripts" / "research_library.py"
    )

    repo_env = tmp_path / "repo.env"
    repo_env.write_text(
        "\n".join(
            [
                "APP_ENV=dalton",
                "DALTON_PROFILE=everflow",
                "DALTON_CAP_SALES_NOTES=false",
                "DALTON_CAP_GUIDEPOINT=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    bridge_env = tmp_path / "bridge.env"
    bridge_env.write_text("DALTON_BRIDGE_TOKEN=r3-mcp-tok\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_DOTENV_PATH", str(repo_env))
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))

    cfg = bridge_mod.BridgeConfig()
    bridge = bridge_mod.ResearchDataBridge(cfg)

    stdio_calls: list[list[dict[str, Any]]] = []

    async def _fake_stdio_rpc(
        requests: list[dict[str, Any]], *, timeout_s: float = 30.0
    ) -> list[dict[str, Any]]:
        _ = timeout_s
        stdio_calls.append(requests)
        payload = {"ok": True, "status": "ok", "results": [], "source_status": {}}
        return [
            {
                "jsonrpc": "2.0",
                "id": requests[0].get("id", 1),
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "structuredContent": payload,
                    "isError": False,
                },
            }
        ]

    ae_calls: list[dict[str, Any]] = []

    async def _fake_ae_fanout(
        req_id: Any,
        ae_args: dict[str, Any],
        timeout_s: float,
        **kwargs: Any,
    ) -> dict[str, Any]:
        _ = (req_id, timeout_s, kwargs)
        ae_calls.append(dict(ae_args))
        return {
            "ok": True,
            "status": "ok",
            "route": "alphaengine",
            "count": 0,
            "results": [],
            "raw_payload": {"ok": True, "results": []},
            "upstream_called": True,
        }

    gp_mock = create_autospec(bridge._run_guidepoint_fanout, side_effect=AssertionError("Guidepoint must not be called"))
    stdio_mock = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_stdio_rpc)
    ae_mock = create_autospec(bridge._run_alphaengine_fanout, side_effect=_fake_ae_fanout)
    monkeypatch.setattr(bridge, "_run_guidepoint_fanout", gp_mock)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", stdio_mock)
    monkeypatch.setattr(bridge, "_run_alphaengine_fanout", ae_mock)

    # 1. Direct disabled route on real /mcp returns HTTP 403 matching JSON payload and calls no connector
    code_direct, body_direct = await _send_real_bridge_mcp_request(
        bridge_mod,
        bridge,
        token="r3-mcp-tok",
        rpc_body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "research", "arguments": {"route": "sales-notes", "query": "NVDA"}},
        },
    )
    assert code_direct == 403
    direct_payload = json.loads(body_direct["result"]["content"][0]["text"])
    assert direct_payload["error_code"] == "capability_disabled"
    assert direct_payload["http_status"] == 403
    assert stdio_mock.await_count == 0

    # 2. Explicit disabled source in route=all params.sources returns HTTP 403 and invokes nothing
    code_exp, body_exp = await _send_real_bridge_mcp_request(
        bridge_mod,
        bridge,
        token="r3-mcp-tok",
        rpc_body={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "research",
                "arguments": {
                    "route": "all",
                    "operation": "search",
                    "query": "NVDA",
                    "params": {"sources": ["company-wiki", "sales-notes"]},
                },
            },
        },
    )
    assert code_exp == 403
    assert json.loads(body_exp["result"]["content"][0]["text"])["error_code"] == "capability_disabled"
    assert stdio_mock.await_count == 0
    assert ae_mock.await_count == 0
    assert gp_mock.await_count == 0

    # 3. route=all search prunes Guidepoint leg AND prunes local sources to enabled subset only
    code_all, body_all = await _send_real_bridge_mcp_request(
        bridge_mod,
        bridge,
        token="r3-mcp-tok",
        rpc_body={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "research",
                "arguments": {"route": "all", "operation": "search", "query": "NVDA"},
            },
        },
    )
    assert code_all == 200
    assert body_all["result"]["isError"] is False
    assert gp_mock.await_count == 0
    assert ae_mock.await_count == 1
    assert stdio_mock.await_count == 1
    local_sent_args = stdio_calls[-1][0]["params"]["arguments"]
    assert local_sent_args["params"]["sources"] == ["company-wiki", "tmtb", "funda-ai"]
    assert "sales-notes" not in local_sent_args["params"]["sources"]

    # 4. route=sales-mail search prunes disabled sales-notes and only queries tmtb with layer=raw
    code_sm, _body_sm = await _send_real_bridge_mcp_request(
        bridge_mod,
        bridge,
        token="r3-mcp-tok",
        rpc_body={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "research",
                "arguments": {"route": "sales-mail", "operation": "search", "query": "META"},
            },
        },
    )
    assert code_sm == 200
    sm_sent_args = stdio_calls[-1][0]["params"]["arguments"]
    assert sm_sent_args["params"]["sources"] == ["tmtb"]
    assert sm_sent_args["params"]["layer"] == "raw"

    # 5. route=sales-mail read of mail:* (disabled sales-notes) returns HTTP 403
    code_sm_read, body_sm_read = await _send_real_bridge_mcp_request(
        bridge_mod,
        bridge,
        token="r3-mcp-tok",
        rpc_body={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "research",
                "arguments": {"route": "sales-mail", "operation": "read", "document": "mail:101"},
            },
        },
    )
    assert code_sm_read == 403
    assert json.loads(body_sm_read["result"]["content"][0]["text"])["error_code"] == "capability_disabled"

    # 6. research_library.validate_request prunes disabled sources from bridge-supplied allowlist and rejects explicit disabled sources
    monkeypatch.setenv("DALTON_ENABLED_LIBRARY_SOURCES", "company-wiki,tmtb,funda-ai")
    _p_all, pruned_sources = rl_mod.validate_request("all", "search", query="NVDA")
    assert pruned_sources == ["company-wiki", "tmtb", "funda-ai"]
    with pytest.raises(ValueError, match="capability_disabled"):
        rl_mod.validate_request("all", "search", query="NVDA", params={"sources": ["sales-notes"]})


@pytest.mark.asyncio
async def test_r3_item2_guidepoint_concurrent_refresh_lock_and_connector_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r3 Item 2: concurrent refresh lock wait honors shared deadline on both :8943 proxy and host_connectors/guidepoint.py, and slow chunked connector response aborts on deadline with in_flight == 0."""
    import threading

    _clean_cap_env(monkeypatch)
    gp_lock_mod = _load_script_module(
        "_test_r3_gp_lock_item2", RESEARCH_RUNTIME_DIR / "guidepoint_lock.py"
    )
    proxy_mod = _load_script_module(
        "_test_r3_gp_proxy_item2", RESEARCH_RUNTIME_DIR / "scripts" / "guidepoint_mcp_proxy.py"
    )
    gp_conn_mod = _load_script_module(
        "_test_r3_gp_conn_item2", RESEARCH_RUNTIME_DIR / "host_connectors" / "guidepoint.py"
    )

    # Part A: :8943 proxy concurrent refresh lock wait respects deadline_mono
    journal_file = tmp_path / "handoff-journal.json"
    token_file = tmp_path / "config" / "guidepoint_mcp_tokens.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(
        json.dumps(
            {
                "access_token": "tok-old",
                "refresh_token": "ref-old",
                "obtained_at": 1.0,  # expired -> triggers refresh
                "expires_in": 600,
            }
        ),
        encoding="utf-8",
    )
    gp_lock_mod.set_target_owner(gp_lock_mod.OWNER_LEGACY_PROXY, ownership_file=journal_file)
    monkeypatch.setenv("DALTON_GUIDEPOINT_OWNERSHIP_FILE", str(journal_file))
    monkeypatch.setenv("GUIDEPOINT_TOKENS_FILE", str(token_file))
    monkeypatch.setenv("GUIDEPOINT_CLIENT_ID", "cid-r3")
    monkeypatch.setenv("GUIDEPOINT_CLIENT_SECRET", "csec-r3")

    refresh_entered = threading.Event()
    release_refresh = threading.Event()

    def _slow_refresh_urlopen(
        url: Any,
        data: Any = None,
        timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
        *,
        context: Any = None,
    ) -> Any:
        _ = (url, data, timeout, context)
        refresh_entered.set()
        release_refresh.wait(timeout=3.0)
        return io.BytesIO(
            json.dumps({"access_token": "tok-new", "refresh_token": "ref-new", "expires_in": 600}).encode("utf-8")
        )

    lock = gp_lock_mod.GuidepointOwnerLock(token_file, service_name="test-r3")
    lock.acquire(timeout_s=2.0)
    proxy_mod._owner_lock = lock
    proxy_mod._standby = False
    proxy_mod._draining = False
    proxy_mod._in_flight = 0
    proxy_mod.load_tokens(token_file)
    try:
        monkeypatch.setattr(
            proxy_mod.urllib.request,
            "urlopen",
            create_autospec(proxy_mod.urllib.request.urlopen, side_effect=_slow_refresh_urlopen),
        )
        t1 = threading.Thread(
            target=lambda: proxy_mod.ensure_fresh_token(deadline_mono=time.monotonic() + 5.0),
            daemon=True,
        )
        t1.start()
        assert refresh_entered.wait(timeout=2.0)

        t0 = time.monotonic()
        with pytest.raises(TimeoutError, match="lock"):
            proxy_mod.ensure_fresh_token(deadline_mono=time.monotonic() + 0.35)
        wait_elapsed = time.monotonic() - t0
        assert wait_elapsed < 1.2, f"Proxy lock wait exceeded deadline: {wait_elapsed:.2f}s"
    finally:
        release_refresh.set()
        t1.join(timeout=2.0)
        proxy_mod.release_owner_lock()

    # Part B: GuidepointTokenManager concurrent async lock wait respects deadline_mono
    gp_lock_mod.set_target_owner(gp_lock_mod.OWNER_RESEARCH_HOST, ownership_file=journal_file)
    token_file.write_text(
        json.dumps(
            {
                "access_token": "tok-async-old",
                "refresh_token": "ref-async-old",
                "obtained_at": 1.0,
                "expires_in": 600,
            }
        ),
        encoding="utf-8",
    )
    async_refresh_entered = asyncio.Event()
    async_refresh_release = asyncio.Event()

    async def _slow_async_refresh_handler(_request: httpx.Request) -> httpx.Response:
        async_refresh_entered.set()
        await asyncio.wait_for(async_refresh_release.wait(), timeout=3.0)
        return httpx.Response(
            200,
            json={"access_token": "tok-async-new", "refresh_token": "ref-async-new", "expires_in": 600},
        )

    orig_async_client = httpx.AsyncClient

    def _refresh_client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_slow_async_refresh_handler)
        return orig_async_client(*args, **kwargs)

    mgr = gp_conn_mod.GuidepointTokenManager(
        token_file,
        client_id="cid-async",
        client_secret="csec-async",
        ownership_file=journal_file,
        http_client_factory=create_autospec(orig_async_client, side_effect=_refresh_client_factory),
    )
    assert mgr.initialize_from_ownership(timeout_s=2.0) is True
    try:
        task1 = asyncio.create_task(mgr.ensure_fresh_token(deadline_mono=time.monotonic() + 5.0))
        await asyncio.wait_for(async_refresh_entered.wait(), timeout=2.0)
        t0_async = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await mgr.ensure_fresh_token(deadline_mono=time.monotonic() + 0.35)
        assert (time.monotonic() - t0_async) < 1.2
        async_refresh_release.set()
        assert await task1 == "tok-async-new"

        # Part C: GuidepointConnector via ResearchHostRuntime aborts slow chunked stream (~10s) at 2s deadline and zeros in_flight
        class _SlowChunkStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for idx in range(20):
                    await asyncio.sleep(0.5)
                    yield f"data: {{\"chunk\": {idx}}}\n\n".encode("utf-8")

        async def _slow_gp_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=_SlowChunkStream())

        def _slow_gp_client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(_slow_gp_handler)
            return orig_async_client(*args, **kwargs)

        conn = gp_conn_mod.GuidepointConnector(
            mgr,
            http_client_factory=create_autospec(orig_async_client, side_effect=_slow_gp_client_factory),
        )
        rt = ResearchHostRuntime(
            ResearchHostConfig(
                data_root=tmp_path / "rh_gp",
                mcp_token="gp-mcp-tok",
                admin_token="gp-adm-tok",
                profile=DeploymentProfile.EVERFLOW,
            ),
            capabilities_provider=lambda: frozenset({CapabilityId.GUIDEPOINT}),
        )
        rt.registry.register_connector(conn)
        app = create_app(runtime=rt)
        async with orig_async_client(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8951"
        ) as client:
            t0_conn = time.monotonic()
            resp = await client.post(
                "/mcp/guidepoint",
                headers={"Authorization": "Bearer gp-mcp-tok"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "search_library", "arguments": {"query": "AI", "timeout": 2}},
                },
            )
            elapsed_conn = time.monotonic() - t0_conn
        assert elapsed_conn < 3.2, f"GuidepointConnector exceeded 2s deadline: {elapsed_conn:.2f}s"
        assert resp.status_code == 200
        body = json.loads(resp.json()["result"]["content"][0]["text"])
        assert body["status"] == "timeout"
        assert body["error_code"] == "upstream_timeout"
        assert rt.in_flight == 0
    finally:
        async_refresh_release.set()
        mgr.release_owner_lock()


@pytest.mark.asyncio
async def test_r3_item3_real_wakeup_executor_effective_attempts_and_503_maintenance_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r3 Item 3: real delivery.execute_wakeup with incrementing attempts does not exhaust on first transient timeout after 6 maintenance deferrals, and handles 503 maintenance_in_progress race via maintenance_gate deferral."""
    from datetime import datetime, timezone
    import aiohttp

    from src.server.database import conversation as conv_db
    from src.server.database import workspace as ws_db
    from src.server.database.runs import lifecycle as tl_db
    from src.server.database.runs import outbox as outbox_db
    from src.server.database.wakeups import fire as fire_db
    from src.server.database.wakeups import riders as riders_db
    from src.server.services import features, hook_outbox
    from src.server.services.report_back import notify_turn
    from src.server.services.wakeups import delivery

    state_file = tmp_path / "maintenance-admission.json"
    monkeypatch.setenv("DALTON_MAINTENANCE_ADMISSION_FILE", str(state_file))
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "svc-tok-r3")
    write_maintenance_state_atomic(
        {
            "version": 1,
            "revision": 1,
            "paused": True,
            "pause_host_direct": True,
            "allow_workspace_ids": [],
            "reason": "maintenance_window",
            "operation": "switch",
            "rehearsal_keep_paused_until": None,
            "updated_at": "2026-11-26T00:00:00+00:00",
        },
        state_file=state_file,
    )

    row: dict[str, Any] = {
        "hook_outbox_id": "job-wake-real-1",
        "hook_type": "wakeup",
        "conversation_thread_id": "thr-wake-real-1",
        "attempts": 0,
        "created_at": datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc),
        "payload": {
            "wakeup_id": "wk-real-1",
            "notice": "fire",
            "seq": 1,
            "user_id": "u-real-1",
        },
    }

    def _claim_next() -> dict[str, Any]:
        row["attempts"] += 1
        return {**row, "payload": dict(row["payload"])}

    async def _reschedule_impl(
        job_id: str,
        *,
        attempts: int,
        delay_seconds: float,
        payload_patch: dict[str, Any] | None = None,
    ) -> str:
        assert job_id == row["hook_outbox_id"]
        assert attempts == row["attempts"]
        assert delay_seconds == 60.0
        row["payload"]["reschedules"] = int(row["payload"].get("reschedules") or 0) + 1
        if payload_patch:
            row["payload"].update(payload_patch)
        return "pending"

    reschedule_mock = create_autospec(outbox_db.reschedule_claimed_job, side_effect=_reschedule_impl)
    nack_mock = create_autospec(outbox_db.nack_outbox_job, return_value="pending")
    ack_mock = create_autospec(outbox_db.ack_outbox_job, return_value=True)
    exhaust_mock = create_autospec(outbox_db.exhaust_alert, return_value=True)
    merge_mock = create_autospec(outbox_db.merge_job_payload, return_value=True)
    release_riders_mock = create_autospec(riders_db.unfreeze_wakeup_riders, return_value=1)

    monkeypatch.setattr(outbox_db, "reschedule_claimed_job", reschedule_mock)
    monkeypatch.setattr(outbox_db, "nack_outbox_job", nack_mock)
    monkeypatch.setattr(outbox_db, "ack_outbox_job", ack_mock)
    monkeypatch.setattr(outbox_db, "exhaust_alert", exhaust_mock)
    monkeypatch.setattr(outbox_db, "merge_job_payload", merge_mock)
    monkeypatch.setattr(outbox_db, "extend_job_lease", create_autospec(outbox_db.extend_job_lease, return_value=True))
    monkeypatch.setattr(tl_db, "get_latest_attempt", create_autospec(tl_db.get_latest_attempt, return_value=None))
    monkeypatch.setattr(fire_db, "job_owed", create_autospec(fire_db.job_owed, return_value=True))
    monkeypatch.setattr(fire_db, "mark_delivered", create_autospec(fire_db.mark_delivered, return_value=True))
    monkeypatch.setattr(delivery, "showing_run", create_autospec(delivery.showing_run, return_value=None))
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(
            conv_db.get_thread_by_id,
            return_value={"workspace_id": "ws-real-1", "archived_at": None},
        ),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value={"workspace_id": "ws-real-1"}),
    )
    monkeypatch.setattr(features, "user_feature_enabled", create_autospec(features.user_feature_enabled, return_value=True))
    monkeypatch.setattr(notify_turn, "park_while_thread_busy", create_autospec(notify_turn.park_while_thread_busy, return_value=False))
    monkeypatch.setattr(notify_turn, "park_while_computer_away", create_autospec(notify_turn.park_while_computer_away, return_value=False))
    monkeypatch.setattr(
        riders_db,
        "gather_wakeup_riders",
        create_autospec(
            riders_db.gather_wakeup_riders,
            return_value=[{"hook_outbox_id": "rider-1", "created_at": row["created_at"], "payload": {"wakeup_id": "wk-r1", "notice": "fire", "seq": 1}}],
        ),
    )
    monkeypatch.setattr(riders_db, "unfreeze_wakeup_riders", release_riders_mock)
    monkeypatch.setattr(riders_db, "settle_wakeup_riders", create_autospec(riders_db.settle_wakeup_riders, return_value=["rider-1"]))
    monkeypatch.setattr(delivery, "render_wake", create_autospec(delivery.render_wake, return_value=("<wakeup/>", [{"seq": 1}])))

    post_outcomes: list[tuple[str, str | None]] = [("cap", None), ("dispatched", "run-recovered-1")]

    async def _fake_post_notification_turn(**_kw: Any) -> tuple[str, str | None]:
        return post_outcomes.pop(0)

    post_mock = create_autospec(notify_turn.post_notification_turn, side_effect=_fake_post_notification_turn)
    monkeypatch.setattr(notify_turn, "post_notification_turn", post_mock)
    monkeypatch.setitem(hook_outbox._EXECUTORS, "wakeup", delivery.execute_wakeup)

    drainer = hook_outbox.HookOutboxDrainer()

    # Scenario 1: 6 consecutive paused claims increment attempts 1..6 and reschedules 1..6
    for expected_attempt in range(1, 7):
        claimed = _claim_next()
        assert claimed["attempts"] == expected_attempt
        await drainer._execute(claimed)

    assert reschedule_mock.await_count == 6
    assert row["payload"]["reschedules"] == 6
    assert nack_mock.await_count == 0
    assert exhaust_mock.await_count == 0
    assert release_riders_mock.await_count == 6

    # Resume maintenance; claim #7 has attempts=7, reschedules=6 => effective_attempts == 1 < MAX_ATTEMPTS
    write_maintenance_state_atomic(
        {
            "version": 1,
            "revision": 2,
            "paused": False,
            "pause_host_direct": False,
            "allow_workspace_ids": [],
            "reason": "resumed",
            "operation": "switch",
            "rehearsal_keep_paused_until": None,
            "updated_at": "2026-11-26T00:05:00+00:00",
        },
        state_file=state_file,
    )
    claim_7 = _claim_next()
    assert claim_7["attempts"] == 7
    assert outbox_db.effective_attempts(claim_7["attempts"], claim_7["payload"]) == 1
    await drainer._execute(claim_7)
    # First transient timeout ("cap") MUST retry via nack, NOT exhaust!
    assert exhaust_mock.await_count == 0
    assert nack_mock.await_count == 1
    assert ack_mock.await_count == 0

    # Claim #8 succeeds and acks
    claim_8 = _claim_next()
    await drainer._execute(claim_8)
    assert ack_mock.await_count == 1
    assert exhaust_mock.await_count == 0

    # Scenario 2: Gate closes AFTER entry check -> real post_notification_turn gets HTTP 503 maintenance_in_progress
    # Restore real post_notification_turn on notify_turn module
    monkeypatch.undo()
    monkeypatch.setenv("DALTON_MAINTENANCE_ADMISSION_FILE", str(state_file))
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "svc-tok-r3")
    monkeypatch.setattr(outbox_db, "reschedule_claimed_job", reschedule_mock)
    monkeypatch.setattr(outbox_db, "nack_outbox_job", nack_mock)
    monkeypatch.setattr(outbox_db, "ack_outbox_job", ack_mock)
    monkeypatch.setattr(outbox_db, "exhaust_alert", exhaust_mock)
    monkeypatch.setattr(outbox_db, "merge_job_payload", merge_mock)
    monkeypatch.setattr(outbox_db, "extend_job_lease", create_autospec(outbox_db.extend_job_lease, return_value=True))
    monkeypatch.setattr(tl_db, "get_latest_attempt", create_autospec(tl_db.get_latest_attempt, return_value=None))
    monkeypatch.setattr(fire_db, "job_owed", create_autospec(fire_db.job_owed, return_value=True))
    monkeypatch.setattr(delivery, "showing_run", create_autospec(delivery.showing_run, return_value=None))
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(
            conv_db.get_thread_by_id,
            return_value={"workspace_id": "ws-real-1", "archived_at": None},
        ),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value={"workspace_id": "ws-real-1"}),
    )
    monkeypatch.setattr(features, "user_feature_enabled", create_autospec(features.user_feature_enabled, return_value=True))
    monkeypatch.setattr(notify_turn, "park_while_thread_busy", create_autospec(notify_turn.park_while_thread_busy, return_value=False))
    monkeypatch.setattr(notify_turn, "park_while_computer_away", create_autospec(notify_turn.park_while_computer_away, return_value=False))
    monkeypatch.setattr(
        riders_db,
        "gather_wakeup_riders",
        create_autospec(
            riders_db.gather_wakeup_riders,
            return_value=[{"hook_outbox_id": "rider-2", "created_at": row["created_at"], "payload": {"wakeup_id": "wk-r2", "notice": "fire", "seq": 1}}],
        ),
    )
    monkeypatch.setattr(riders_db, "unfreeze_wakeup_riders", release_riders_mock)
    monkeypatch.setattr(delivery, "render_wake", create_autospec(delivery.render_wake, return_value=("<wakeup/>", [{"seq": 1}])))
    monkeypatch.setitem(hook_outbox._EXECUTORS, "wakeup", delivery.execute_wakeup)

    post_counter_r3: list[int] = []
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        _make_autospec_503_maintenance_session_factory(post_counter_r3),
    )

    nack_before = nack_mock.await_count
    reschedule_before = reschedule_mock.await_count
    riders_before = release_riders_mock.await_count
    claim_race = _claim_next()
    await drainer._execute(claim_race)

    assert len(post_counter_r3) == 1, "503 maintenance_in_progress must defer immediately instead of 5xx retry loop"
    assert reschedule_mock.await_count == reschedule_before + 1
    assert nack_mock.await_count == nack_before
    assert exhaust_mock.await_count == 0
    assert merge_mock.await_count == 0
    assert release_riders_mock.await_count == riders_before + 1


def test_r3_item4_public_research_host_stripped_and_everflow_ext_provides_legacy_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r3 Item 4: public research_host contains no Everflow default dir, legacy bridge URL/credentials, or sales_mail comments; Everflow host_connectors supplies them."""
    import dataclasses
    from src.dalton.research_host.__main__ import ALLOWED_HOST_ENV_KEYS
    from src.dalton.research_host.connector_registry import ConnectorRegistry

    _clean_cap_env(monkeypatch)

    # 1. Public ResearchHostConfig has no legacy bridge fields and defaults to /var/lib/dalton/wiki
    field_names = {f.name for f in dataclasses.fields(ResearchHostConfig)}
    assert "bridge_admin_token" not in field_names
    assert "legacy_bridge_mcp_url" not in field_names
    assert "legacy_bridge_token" not in field_names
    public_cfg = ResearchHostConfig.from_env({})
    assert public_cfg.data_root == Path("/var/lib/dalton/wiki").resolve()

    # 2. Public ALLOWED_HOST_ENV_KEYS has no legacy bridge credential/URL keys
    for removed_key in (
        "DALTON_BRIDGE_ADMIN_TOKEN",
        "DALTON_LEGACY_BRIDGE_MCP_URL",
        "DALTON_BRIDGE_TOKEN",
    ):
        assert removed_key not in ALLOWED_HOST_ENV_KEYS

    # 3. Public source files contain no Everflow default dir, legacy bridge env keys, or sales_mail comment
    app_src = (REPO_ROOT / "src" / "dalton" / "research_host" / "app.py").read_text(encoding="utf-8")
    main_src = (REPO_ROOT / "src" / "dalton" / "research_host" / "__main__.py").read_text(encoding="utf-8")
    reg_src = (REPO_ROOT / "src" / "dalton" / "research_host" / "connector_registry.py").read_text(encoding="utf-8")
    assert "Everflow" not in app_src
    assert "DALTON_LEGACY_BRIDGE_MCP_URL" not in app_src and "DALTON_LEGACY_BRIDGE_MCP_URL" not in main_src
    assert "DALTON_BRIDGE_ADMIN_TOKEN" not in app_src and "DALTON_BRIDGE_ADMIN_TOKEN" not in main_src
    assert "DALTON_BRIDGE_TOKEN" not in app_src and "DALTON_BRIDGE_TOKEN" not in main_src
    assert "sales_mail" not in reg_src

    # 4. Private host_connectors extension supplies Everflow default dir, legacy bridge keys, and caller secret registration
    hc_cfg = ResearchHostConfig.from_env(
        {
            "DALTON_PROFILE": "everflow",
            "DALTON_RESEARCH_HOST_EXTRA_MODULES": "host_connectors",
            "DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH": str(RESEARCH_RUNTIME_DIR),
        }
    )
    assert hc_cfg.data_root == (Path.home() / "Library" / "Application Support" / "Everflow" / "research").resolve()

    import host_connectors

    for priv_key in (
        "DALTON_BRIDGE_ADMIN_TOKEN",
        "DALTON_LEGACY_BRIDGE_MCP_URL",
        "DALTON_BRIDGE_TOKEN",
    ):
        assert priv_key in host_connectors.EXTRA_HOST_ENV_KEYS

    reg = ConnectorRegistry()
    ext_cfg = ResearchHostConfig(
        data_root=tmp_path,
        mcp_token="mcp-ef",
        admin_token="adm-ef",
        profile=DeploymentProfile.EVERFLOW,
        environ={
            "DALTON_BRIDGE_ADMIN_TOKEN": "bridge-adm-secret-r3",
            "DALTON_LEGACY_BRIDGE_MCP_URL": "http://127.0.0.1:8960/mcp",
            "DALTON_BRIDGE_TOKEN": "bridge-mcp-tok-r3",
        },
    )
    host_connectors.register(reg, ext_cfg, active_caps=frozenset({CapabilityId.ALPHAENGINE}))
    assert "bridge-adm-secret-r3" in reg.caller_admin_secrets
    assert "alphaengine" in reg.federated_sources

    # 5. Out-of-package release scan wordlist stays at zero hits across public source dirs
    checker = _load_script_module(
        "_test_r3_release_checker_item4",
        SCRIPTS_DALTON_DIR / "ops" / "check_core_release_package.py",
    )
    report = checker.check_core_release_package(repo_root=REPO_ROOT)
    assert report.public_source_hits == []


def _make_autospec_503_maintenance_session_factory(
    post_counter: list[int],
):
    """Build an autospec aiohttp.ClientSession factory returning 503 maintenance_in_progress."""
    import aiohttp

    orig_session_cls = aiohttp.ClientSession
    orig_resp_cls = aiohttp.ClientResponse

    def _factory() -> aiohttp.ClientSession:
        resp_mock = create_autospec(orig_resp_cls, instance=True)
        resp_mock.status = 503
        resp_mock.json.return_value = {
            "detail": {
                "ok": False,
                "status": "maintenance_in_progress",
                "error": "maintenance_in_progress",
                "error_code": "maintenance_in_progress",
            }
        }
        resp_mock.__aenter__.return_value = resp_mock
        resp_mock.__aexit__.return_value = False

        def _post_side_effect(*_args: Any, **_kwargs: Any) -> aiohttp.ClientResponse:
            post_counter.append(1)
            return resp_mock

        session_mock = create_autospec(orig_session_cls, instance=True)
        session_mock.__aenter__.return_value = session_mock
        session_mock.__aexit__.return_value = False
        session_mock.post.side_effect = _post_side_effect
        return session_mock

    return create_autospec(orig_session_cls, side_effect=_factory)


@pytest.mark.asyncio
async def test_r4_item1_real_subprocess_chain_never_reloads_shared_env_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 Item 1: real subprocess chain (bridge -> researchdata_mcp -> data_gateway -> research_library) never reloads shared .env secrets and receives only allowed source names."""
    import sqlite3

    _clean_cap_env(monkeypatch)
    bridge_mod = _load_script_module(
        "_test_r4_bridge_item1", SCRIPTS_DALTON_DIR / "researchdata_bridge.py"
    )

    rt_code = tmp_path / "rt_code"
    rt_home = tmp_path / "rt_home"
    (rt_code / "scripts").mkdir(parents=True)
    (rt_code / "config").mkdir(parents=True)
    (rt_code / "skills" / "company-wiki" / "scripts").mkdir(parents=True)
    (rt_home / "wiki").mkdir(parents=True)
    venv_bin = rt_home / "venvs" / "market-digest" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(Path(sys.executable).resolve())

    for fname in (
        "researchdata_mcp.py",
        "data_gateway.py",
        "research_library.py",
        "research_library.sh",
        "research_images.py",
    ):
        dst = rt_code / "scripts" / fname
        shutil.copy2(RESEARCH_RUNTIME_DIR / "scripts" / fname, dst)
        if fname.endswith(".sh"):
            dst.chmod(0o755)
    shutil.copy2(
        RESEARCH_RUNTIME_DIR / "config" / "data_sources.json",
        rt_code / "config" / "data_sources.json",
    )

    env_dump_path = tmp_path / "child_library_env.json"
    (rt_code / "skills" / "company-wiki" / "config.json").write_text("{}", encoding="utf-8")
    (rt_code / "skills" / "company-wiki" / "scripts" / "company_wiki_cli.py").write_text(
        f"""
import json, os, sys
from pathlib import Path

def build_doc_filters(_ct, _cn, _start, _end, _tag):
    Path({str(env_dump_path)!r}).write_text(
        json.dumps(
            {{
                "env": dict(os.environ),
                "loaded_src_config": "src.config" in sys.modules,
                "loaded_src_caps": "src.dalton.capabilities" in sys.modules,
            }},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return [], []
""".lstrip(),
        encoding="utf-8",
    )

    wiki_db = rt_home / "wiki" / "vectors.db"
    with sqlite3.connect(wiki_db) as conn:
        conn.execute(
            "CREATE TABLE documents ("
            "id INTEGER PRIMARY KEY, filepath TEXT, filename TEXT, date TEXT, "
            "created_at TEXT, category_type TEXT, category_name TEXT, tags TEXT)"
        )
        conn.commit()

    shared_env = tmp_path / "shared.env"
    shared_env.write_text(
        "\n".join(
            [
                "APP_ENV=dalton",
                "DALTON_PROFILE=everflow",
                "DALTON_CAP_SALES_NOTES=false",
                "SUPABASE_SECRET_KEY=supa-secret-r4-must-not-leak",
                "DALTON_RESEARCH_ADMIN_TOKEN=admin-tok-r4-must-not-leak",
                "DB_PASSWORD=db-pass-r4-must-not-leak",
                "POSTGRES_PASSWORD=pg-pass-r4-must-not-leak",
                "REDIS_PASSWORD=redis-pass-r4-must-not-leak",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    bridge_env = tmp_path / "bridge.env"
    bridge_env.write_text("DALTON_BRIDGE_TOKEN=br-tok-r4\n", encoding="utf-8")

    monkeypatch.setenv("DALTON_DOTENV_PATH", str(shared_env))
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))
    monkeypatch.setenv("DALTON_RESEARCH_CODE", str(rt_code))
    monkeypatch.setenv("DALTON_RESEARCH_HOME", str(rt_home))
    # Also plant secrets in bridge process env to verify both parent stripping and non-reloading
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "supa-secret-r4-parent")
    monkeypatch.setenv("DALTON_RESEARCH_ADMIN_TOKEN", "admin-tok-r4-parent")
    monkeypatch.setenv("DB_PASSWORD", "db-pass-r4-parent")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pg-pass-r4-parent")
    monkeypatch.setenv("REDIS_PASSWORD", "redis-pass-r4-parent")

    cfg = bridge_mod.BridgeConfig()
    cfg.python_bin = sys.executable
    cfg.mcp_script = rt_code / "scripts" / "researchdata_mcp.py"
    cfg.research_code = rt_code
    cfg.research_home = rt_home
    bridge = bridge_mod.ResearchDataBridge(cfg)

    # 1. Real subprocess chain executes company-wiki list -> research_library.py runs and dumps env
    resps = await bridge._run_stdio_rpc(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "research",
                    "arguments": {
                        "route": "company-wiki",
                        "operation": "list",
                        "params": {"media": "text"},
                    },
                },
            }
        ],
        timeout_s=15.0,
    )
    assert resps
    payload = json.loads(resps[0]["result"]["content"][0]["text"])
    assert payload["ok"] is True
    assert env_dump_path.is_file(), "research_library.py child did not execute"
    dumped = json.loads(env_dump_path.read_text(encoding="utf-8"))
    child_env = dumped["env"]
    assert child_env.get("DALTON_ENABLED_LIBRARY_SOURCES") == "company-wiki,tmtb,funda-ai"
    assert dumped["loaded_src_config"] is False
    assert dumped["loaded_src_caps"] is False
    for secret_key in (
        "SUPABASE_SECRET_KEY",
        "DALTON_RESEARCH_ADMIN_TOKEN",
        "DB_PASSWORD",
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "DALTON_BRIDGE_TOKEN",
        "DALTON_BRIDGE_ADMIN_TOKEN",
    ):
        assert secret_key not in child_env, f"Secret {secret_key} leaked into library subprocess env"

    # 2. Gateway enforces disabled source from bridge-supplied DALTON_ENABLED_LIBRARY_SOURCES
    disabled_resps = await bridge._run_stdio_rpc(
        [
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "research",
                    "arguments": {
                        "route": "sales-notes",
                        "operation": "list",
                        "params": {"media": "text"},
                    },
                },
            }
        ],
        timeout_s=15.0,
    )
    disabled_payload = json.loads(disabled_resps[0]["result"]["content"][0]["text"])
    assert disabled_payload["ok"] is False
    assert "capability_disabled" in str(disabled_payload.get("error") or "")


@pytest.mark.enable_socket
def test_r4_item2_proxy_8943_deadline_on_slow_headers_split_chunks_and_blocked_downstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 Item 2: :8943 proxy enforces one absolute deadline across slow response headers, split chunk boundaries, and blocked downstream writes, always zeroing _in_flight."""
    import http.client
    import threading

    _clean_cap_env(monkeypatch)
    gp_lock_mod = _load_script_module(
        "_test_r4_gp_lock_item2", RESEARCH_RUNTIME_DIR / "guidepoint_lock.py"
    )
    proxy_mod = _load_script_module(
        "_test_r4_gp_proxy_item2", RESEARCH_RUNTIME_DIR / "scripts" / "guidepoint_mcp_proxy.py"
    )

    journal_file = tmp_path / "handoff-journal.json"
    token_file = tmp_path / "config" / "guidepoint_mcp_tokens.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(
        json.dumps(
            {
                "access_token": "tok-r4-dl",
                "refresh_token": "ref-r4-dl",
                "obtained_at": time.time(),
                "expires_in": 600,
            }
        ),
        encoding="utf-8",
    )
    gp_lock_mod.set_target_owner(gp_lock_mod.OWNER_LEGACY_PROXY, ownership_file=journal_file)
    monkeypatch.setenv("DALTON_GUIDEPOINT_OWNERSHIP_FILE", str(journal_file))
    monkeypatch.setenv("GUIDEPOINT_TOKENS_FILE", str(token_file))

    req_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search_library",
                "arguments": {"query": "semis", "size": 1, "timeout": 1},
            },
        }
    ).encode("utf-8")
    raw_http_req = (
        b"POST /mcp HTTP/1.1\r\n"
        b"Host: 127.0.0.1:8943\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(req_body)}\r\n\r\n".encode("ascii")
        + req_body
    )

    orig_urlopen = proxy_mod.urllib.request.urlopen
    proxy_mod.initialize_runtime(ownership_file=journal_file, token_store=token_file)
    try:
        # Case A: Slow response headers dripping every 0.2s (well under 1s idle timeout) for 6s
        stop_a = threading.Event()
        headers_dripped = 0

        def _urlopen_slow_headers(
            url: Any,
            data: Any = None,
            timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
            *,
            context: Any = None,
        ) -> Any:
            _ = (url, data, context)
            up_cli, up_srv = socket.socketpair()
            up_cli.settimeout(float(timeout) if isinstance(timeout, (int, float)) else 1.0)

            def _drip_headers() -> None:
                nonlocal headers_dripped
                try:
                    up_srv.sendall(b"HTTP/1.1 200 OK\r\n")
                    for idx in range(30):
                        if stop_a.wait(0.2):
                            break
                        up_srv.sendall(f"X-Slow-{idx}: v\r\n".encode("ascii"))
                        headers_dripped += 1
                    up_srv.sendall(b"\r\n")
                except OSError:
                    pass
                finally:
                    try:
                        up_srv.close()
                    except OSError:
                        pass

            threading.Thread(target=_drip_headers, daemon=True).start()
            resp = http.client.HTTPResponse(up_cli)
            resp.begin()
            return resp

        monkeypatch.setattr(
            proxy_mod.urllib.request,
            "urlopen",
            create_autospec(orig_urlopen, side_effect=_urlopen_slow_headers),
        )
        cli_a, srv_a = socket.socketpair()
        cli_a.settimeout(4.0)
        cli_a.sendall(raw_http_req)
        cli_a.shutdown(socket.SHUT_WR)
        rec_a = bytearray()

        def _drain_a() -> None:
            try:
                while True:
                    part = cli_a.recv(4096)
                    if not part:
                        break
                    rec_a.extend(part)
            except OSError:
                pass
            finally:
                cli_a.close()

        ta = threading.Thread(target=_drain_a, daemon=True)
        ta.start()
        t0_a = time.monotonic()
        try:
            proxy_mod.ProxyHandler(srv_a, ("127.0.0.1", 0), None)
        finally:
            try:
                srv_a.close()
            except OSError:
                pass
        elapsed_a = time.monotonic() - t0_a
        stop_a.set()
        ta.join(timeout=2.0)
        assert elapsed_a < 2.2, f"Slow headers exceeded 1s deadline: {elapsed_a:.2f}s"
        assert headers_dripped < 15
        assert b"504" in bytes(rec_a)
        assert b"upstream_timeout" in bytes(rec_a)
        # Wait briefly for mocked-urlopen worker thread to observe stop_a and run its in_flight release callback
        dl_a = time.monotonic() + 1.0
        while proxy_mod.health_payload()["in_flight"] != 0 and time.monotonic() < dl_a:
            time.sleep(0.01)
        assert proxy_mod.health_payload()["in_flight"] == 0

        # Case B: Split chunk boundaries arriving in sub-chunk pieces every 0.2s
        stop_b = threading.Event()
        pieces_sent = 0

        def _urlopen_split_chunks(
            url: Any,
            data: Any = None,
            timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
            *,
            context: Any = None,
        ) -> Any:
            _ = (url, data, context)
            up_cli, up_srv = socket.socketpair()
            up_cli.settimeout(float(timeout) if isinstance(timeout, (int, float)) else 1.0)

            def _drip_split_chunks() -> None:
                nonlocal pieces_sent
                try:
                    up_srv.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/event-stream\r\n"
                        b"Transfer-Encoding: chunked\r\n\r\n"
                    )
                    # Drip chunk-size hex, CRLF, body fragments, and trailing CRLF across boundaries
                    parts = [b"1", b"0", b"\r", b"\n", b"aaaa", b"bbbb", b"cccc", b"dddd", b"\r", b"\n"]
                    for _round in range(6):
                        for piece in parts:
                            if stop_b.wait(0.2):
                                return
                            up_srv.sendall(piece)
                            pieces_sent += 1
                    up_srv.sendall(b"0\r\n\r\n")
                except OSError:
                    pass
                finally:
                    try:
                        up_srv.close()
                    except OSError:
                        pass

            threading.Thread(target=_drip_split_chunks, daemon=True).start()
            resp = http.client.HTTPResponse(up_cli)
            resp.begin()
            return resp

        monkeypatch.setattr(
            proxy_mod.urllib.request,
            "urlopen",
            create_autospec(orig_urlopen, side_effect=_urlopen_split_chunks),
        )
        cli_b, srv_b = socket.socketpair()
        cli_b.settimeout(4.0)
        cli_b.sendall(raw_http_req)
        cli_b.shutdown(socket.SHUT_WR)
        rec_b = bytearray()

        def _drain_b() -> None:
            try:
                while True:
                    part = cli_b.recv(4096)
                    if not part:
                        break
                    rec_b.extend(part)
            except OSError:
                pass
            finally:
                cli_b.close()

        tb = threading.Thread(target=_drain_b, daemon=True)
        tb.start()
        t0_b = time.monotonic()
        try:
            proxy_mod.ProxyHandler(srv_b, ("127.0.0.1", 0), None)
        finally:
            try:
                srv_b.close()
            except OSError:
                pass
        elapsed_b = time.monotonic() - t0_b
        stop_b.set()
        tb.join(timeout=2.0)
        assert elapsed_b < 2.2, f"Split chunk boundaries exceeded 1s deadline: {elapsed_b:.2f}s"
        assert pieces_sent < 25
        dl_b = time.monotonic() + 1.0
        while proxy_mod.health_payload()["in_flight"] != 0 and time.monotonic() < dl_b:
            time.sleep(0.01)
        assert proxy_mod.health_payload()["in_flight"] == 0

        # Case C: Downstream write blocked (client never reads, kernel buffer fills)
        stop_c = threading.Event()

        def _urlopen_flood_body(
            url: Any,
            data: Any = None,
            timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
            *,
            context: Any = None,
        ) -> Any:
            _ = (url, data, context)
            up_cli, up_srv = socket.socketpair()
            up_cli.settimeout(float(timeout) if isinstance(timeout, (int, float)) else 1.0)

            def _flood() -> None:
                try:
                    up_srv.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/octet-stream\r\n"
                        b"Content-Length: 16777216\r\n\r\n"
                    )
                    block = b"X" * 65536
                    for _ in range(256):
                        if stop_c.is_set():
                            break
                        up_srv.sendall(block)
                except OSError:
                    pass
                finally:
                    try:
                        up_srv.close()
                    except OSError:
                        pass

            threading.Thread(target=_flood, daemon=True).start()
            resp = http.client.HTTPResponse(up_cli)
            resp.begin()
            return resp

        monkeypatch.setattr(
            proxy_mod.urllib.request,
            "urlopen",
            create_autospec(orig_urlopen, side_effect=_urlopen_flood_body),
        )
        cli_c, srv_c = socket.socketpair()
        try:
            srv_c.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
            cli_c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
        except OSError:
            pass
        cli_c.sendall(raw_http_req)
        cli_c.shutdown(socket.SHUT_WR)
        # Deliberately NEVER read from cli_c so srv_c's kernel send buffer fills and blocks writes
        t0_c = time.monotonic()
        try:
            proxy_mod.ProxyHandler(srv_c, ("127.0.0.1", 0), None)
        finally:
            try:
                srv_c.close()
            except OSError:
                pass
            stop_c.set()
            try:
                cli_c.close()
            except OSError:
                pass
        elapsed_c = time.monotonic() - t0_c
        assert elapsed_c < 2.2, f"Blocked downstream write exceeded 1s deadline: {elapsed_c:.2f}s"
        dl_c = time.monotonic() + 1.0
        while proxy_mod.health_payload()["in_flight"] != 0 and time.monotonic() < dl_c:
            time.sleep(0.01)
        assert proxy_mod.health_payload()["in_flight"] == 0
    finally:
        proxy_mod.release_owner_lock()


@pytest.mark.asyncio
async def test_r4_item3_sales_mail_explicit_sources_allowlist_and_403_via_real_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 Item 3: real /mcp accepts sales-mail params.sources (sales-notes/tmtb), returns HTTP 403 on disabled explicit source with 0 connector calls, and preserves legal subset."""
    _clean_cap_env(monkeypatch)
    bridge_mod = _load_script_module(
        "_test_r4_bridge_item3", SCRIPTS_DALTON_DIR / "researchdata_bridge.py"
    )

    repo_env = tmp_path / "repo.env"
    repo_env.write_text(
        "\n".join(
            [
                "APP_ENV=dalton",
                "DALTON_PROFILE=everflow",
                "DALTON_CAP_SALES_NOTES=false",
                "DALTON_CAP_TMTB=true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    bridge_env = tmp_path / "bridge.env"
    bridge_env.write_text("DALTON_BRIDGE_TOKEN=r4-sm-tok\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_DOTENV_PATH", str(repo_env))
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))

    cfg = bridge_mod.BridgeConfig()
    bridge = bridge_mod.ResearchDataBridge(cfg)

    stdio_calls: list[list[dict[str, Any]]] = []

    async def _fake_stdio_rpc(
        requests: list[dict[str, Any]], *, timeout_s: float = 30.0
    ) -> list[dict[str, Any]]:
        _ = timeout_s
        stdio_calls.append(requests)
        payload = {"ok": True, "status": "ok", "results": [], "source_status": {}}
        return [
            {
                "jsonrpc": "2.0",
                "id": requests[0].get("id", 1),
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "structuredContent": payload,
                    "isError": False,
                },
            }
        ]

    stdio_mock = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_stdio_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", stdio_mock)

    # 1. Explicit disabled source in sales-mail params.sources -> HTTP 403, 0 connector calls
    for req_id, req_sources in ((1, ["sales-notes"]), (2, ["sales-notes", "tmtb"])):
        code, body = await _send_real_bridge_mcp_request(
            bridge_mod,
            bridge,
            token="r4-sm-tok",
            rpc_body={
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {
                    "name": "research",
                    "arguments": {
                        "route": "sales-mail",
                        "operation": "search",
                        "query": "NVDA",
                        "params": {"sources": req_sources},
                    },
                },
            },
        )
        assert code == 403, f"Expected HTTP 403 for sales-mail sources={req_sources}, got {code}: {body}"
        parsed = json.loads(body["result"]["content"][0]["text"])
        assert parsed["error_code"] == "capability_disabled"
        assert parsed["http_status"] == 403
        assert stdio_mock.await_count == 0

    # 2. Legal enabled subset ["tmtb"] executes normally with layer=raw
    code_ok, body_ok = await _send_real_bridge_mcp_request(
        bridge_mod,
        bridge,
        token="r4-sm-tok",
        rpc_body={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "research",
                "arguments": {
                    "route": "sales-mail",
                    "operation": "search",
                    "query": "NVDA",
                    "params": {"sources": ["tmtb"]},
                },
            },
        },
    )
    assert code_ok == 200
    assert body_ok["result"]["isError"] is False
    assert stdio_mock.await_count == 1
    sent_args = stdio_calls[-1][0]["params"]["arguments"]
    assert sent_args["params"]["sources"] == ["tmtb"]
    assert sent_args["params"]["layer"] == "raw"

    # 3. With both sales-notes and tmtb enabled, explicit subset ["tmtb"] is preserved on search & list, and unknown source is rejected
    repo_env.write_text(
        "APP_ENV=dalton\nDALTON_PROFILE=everflow\nDALTON_CAP_SALES_NOTES=true\nDALTON_CAP_TMTB=true\n",
        encoding="utf-8",
    )
    cfg2 = bridge_mod.BridgeConfig()
    bridge2 = bridge_mod.ResearchDataBridge(cfg2)
    stdio_calls2: list[list[dict[str, Any]]] = []

    async def _fake_stdio_rpc2(
        requests: list[dict[str, Any]], *, timeout_s: float = 30.0
    ) -> list[dict[str, Any]]:
        _ = timeout_s
        stdio_calls2.append(requests)
        payload = {"ok": True, "status": "ok", "results": [], "source_status": {}}
        return [
            {
                "jsonrpc": "2.0",
                "id": requests[0].get("id", 1),
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "structuredContent": payload,
                    "isError": False,
                },
            }
        ]

    stdio_mock2 = create_autospec(bridge2._run_stdio_rpc, side_effect=_fake_stdio_rpc2)
    monkeypatch.setattr(bridge2, "_run_stdio_rpc", stdio_mock2)

    code_sub, _ = await _send_real_bridge_mcp_request(
        bridge_mod,
        bridge2,
        token="r4-sm-tok",
        rpc_body={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "research",
                "arguments": {
                    "route": "sales-mail",
                    "operation": "list",
                    "params": {"sources": ["tmtb"]},
                },
            },
        },
    )
    assert code_sub == 200
    assert stdio_calls2[-1][0]["params"]["arguments"]["params"]["sources"] == ["tmtb"]
    assert stdio_calls2[-1][0]["params"]["arguments"]["params"]["layer"] == "raw"

    code_bad, body_bad = await _send_real_bridge_mcp_request(
        bridge_mod,
        bridge2,
        token="r4-sm-tok",
        rpc_body={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "research",
                "arguments": {
                    "route": "sales-mail",
                    "operation": "search",
                    "query": "NVDA",
                    "params": {"sources": ["company-wiki"]},
                },
            },
        },
    )
    assert code_bad == 200
    bad_payload = json.loads(body_bad["result"]["content"][0]["text"])
    assert bad_payload["ok"] is False
    assert bad_payload["error_code"] == "invalid_arguments"
    assert stdio_mock2.await_count == 1


@pytest.mark.asyncio
async def test_r4_item4_handoff_executor_defers_on_503_maintenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 Item 4 (handoff/executor.py): 503 maintenance_in_progress defers via maintenance_gate without nack, failure count, or watch teardown."""
    import aiohttp

    from src.server.database.runs import outbox as outbox_db
    from src.server.services import hook_outbox
    from src.server.services.report_back import notify_turn
    from src.server.services.report_back.handoff import executor as handoff_exec
    from src.server.services.report_back.handoff import pointer as handoff_pointer
    from src.utils.cache import redis_cache

    state_file = tmp_path / "maintenance-admission.json"
    monkeypatch.setenv("DALTON_MAINTENANCE_ADMISSION_FILE", str(state_file))
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "svc-tok-r4-handoff")

    reschedule_mock = create_autospec(outbox_db.reschedule_claimed_job, return_value="pending")
    nack_mock = create_autospec(outbox_db.nack_outbox_job, return_value="pending")
    ack_mock = create_autospec(outbox_db.ack_outbox_job, return_value=True)
    clear_mock = create_autospec(handoff_pointer.clear_handoff_report_back, return_value=True)

    monkeypatch.setattr(outbox_db, "reschedule_claimed_job", reschedule_mock)
    monkeypatch.setattr(outbox_db, "nack_outbox_job", nack_mock)
    monkeypatch.setattr(outbox_db, "ack_outbox_job", ack_mock)
    monkeypatch.setattr(outbox_db, "extend_job_lease", create_autospec(outbox_db.extend_job_lease, return_value=True))
    monkeypatch.setattr(handoff_pointer, "clear_handoff_report_back", clear_mock)
    monkeypatch.setattr(redis_cache, "get_cache_client", create_autospec(redis_cache.get_cache_client, return_value=object()))

    from src.server.models.chat import AnalystReport

    owed = handoff_exec.OwedAnalystReport(
        ptc_thread_id="ptc-thr-1",
        dispatcher_thread_id="home-thr-1",
        origin={"user_id": "u-ho-1", "origin_workspace_id": "ws-home-1", "dispatch_gen": "gen-1"},
        final_status="completed",
        run_id="run-ptc-1",
    )
    monkeypatch.setattr(
        handoff_exec,
        "analyst_report_verdict",
        create_autospec(handoff_exec.analyst_report_verdict, return_value=owed),
    )
    monkeypatch.setattr(
        handoff_exec,
        "is_resumable_analyst_failure",
        create_autospec(handoff_exec.is_resumable_analyst_failure, return_value=False),
    )
    monkeypatch.setattr(
        handoff_exec,
        "_wait_for_hand_off",
        create_autospec(handoff_exec._wait_for_hand_off, return_value=True),
    )
    monkeypatch.setattr(
        handoff_exec,
        "_hold_report_for_quiescence",
        create_autospec(handoff_exec._hold_report_for_quiescence, return_value="deliver"),
    )
    monkeypatch.setattr(
        handoff_exec,
        "_superseded",
        create_autospec(handoff_exec._superseded, return_value=False),
    )
    report_src = create_autospec(AnalystReport, instance=True)
    report_src.model_dump.return_value = {"status": "completed"}
    monkeypatch.setattr(
        handoff_exec,
        "report_message",
        create_autospec(
            handoff_exec.report_message,
            return_value=("summary", report_src),
        ),
    )
    monkeypatch.setattr(notify_turn, "park_while_thread_busy", create_autospec(notify_turn.park_while_thread_busy, return_value=False))
    monkeypatch.setattr(notify_turn, "park_while_computer_away", create_autospec(notify_turn.park_while_computer_away, return_value=False))

    post_counter: list[int] = []
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        _make_autospec_503_maintenance_session_factory(post_counter),
    )
    monkeypatch.setitem(hook_outbox._EXECUTORS, "handoff_report_back", handoff_exec.execute_report_back)

    job = {
        "hook_outbox_id": "job-ho-503",
        "hook_type": "handoff_report_back",
        "ordering_key": "home-thr-1",
        "attempts": 1,
        "run_id": "run-ptc-1",
        "payload": {"ptc_thread_id": "ptc-thr-1", "dispatch_gen": "gen-1"},
    }
    drainer = hook_outbox.HookOutboxDrainer()
    await drainer._execute(job)

    assert len(post_counter) == 1
    assert reschedule_mock.await_count == 1
    assert nack_mock.await_count == 0
    assert ack_mock.await_count == 0
    assert clear_mock.await_count == 0


@pytest.mark.asyncio
async def test_r4_item4_task_steer_defers_on_503_maintenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 Item 4 (task_steer.py): 503 maintenance_in_progress defers via maintenance_gate without nack, lap increment, or terminal wait."""
    import aiohttp

    from src.server.database.runs import outbox as outbox_db
    from src.server.services import hook_outbox
    from src.server.services.report_back import notify_turn, task_steer

    state_file = tmp_path / "maintenance-admission.json"
    monkeypatch.setenv("DALTON_MAINTENANCE_ADMISSION_FILE", str(state_file))
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "svc-tok-r4-tsteer")

    payload = {
        "target": "task",
        "task_id": "k7Xm2p",
        "content": "continue research",
        "user_id": "u-ts-1",
        "workspace_id": "ws-ts-1",
    }
    row = {
        "id": "job-ts-503",
        "status": "claimed",
        "attempts": 1,
        "payload": payload,
    }
    reschedule_mock = create_autospec(outbox_db.reschedule_claimed_job, return_value="pending")
    nack_mock = create_autospec(outbox_db.nack_outbox_job, return_value="pending")
    ack_mock = create_autospec(outbox_db.ack_outbox_job, return_value=True)
    merge_mock = create_autospec(outbox_db.merge_job_payload, return_value=True)
    await_term_mock = create_autospec(notify_turn.await_run_terminal, return_value="terminal")

    monkeypatch.setattr(outbox_db, "reschedule_claimed_job", reschedule_mock)
    monkeypatch.setattr(outbox_db, "nack_outbox_job", nack_mock)
    monkeypatch.setattr(outbox_db, "ack_outbox_job", ack_mock)
    monkeypatch.setattr(outbox_db, "merge_job_payload", merge_mock)
    monkeypatch.setattr(outbox_db, "get_steer_job", create_autospec(outbox_db.get_steer_job, return_value=row))
    monkeypatch.setattr(outbox_db, "extend_job_lease", create_autospec(outbox_db.extend_job_lease, return_value=True))
    monkeypatch.setattr(task_steer, "_divert", create_autospec(task_steer._divert, return_value=False))
    monkeypatch.setattr(task_steer, "turn_owner", create_autospec(task_steer.turn_owner, return_value=("u-ts-1", "ws-ts-1")))
    monkeypatch.setattr(task_steer, "_computer_full", create_autospec(task_steer._computer_full, return_value=False))
    monkeypatch.setattr(notify_turn, "park_unless_ready", create_autospec(notify_turn.park_unless_ready, return_value=False))
    monkeypatch.setattr(notify_turn, "await_run_terminal", await_term_mock)

    post_counter: list[int] = []
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        _make_autospec_503_maintenance_session_factory(post_counter),
    )
    monkeypatch.setitem(hook_outbox._EXECUTORS, "steer", task_steer.execute_task_steer)

    job = {
        "hook_outbox_id": "job-ts-503",
        "hook_type": "steer",
        "idempotency_key": "steer:ts-503",
        "ordering_key": "thr-ts-1",
        "conversation_thread_id": "thr-ts-1",
        "attempts": 1,
        "payload": payload,
    }
    drainer = hook_outbox.HookOutboxDrainer()
    await drainer._execute(job)

    assert len(post_counter) == 1
    assert reschedule_mock.await_count == 1
    assert nack_mock.await_count == 0
    assert ack_mock.await_count == 0
    assert merge_mock.await_count == 0
    assert await_term_mock.await_count == 0


@pytest.mark.asyncio
async def test_r4_item4_steer_executor_defers_on_503_maintenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 Item 4 (steer_executor.py): 503 maintenance_in_progress defers immediately via maintenance_gate without waiting in _settle_dispatched or counting failure."""
    import aiohttp

    from src.server.database.runs import lifecycle as tl_db
    from src.server.database.runs import outbox as outbox_db
    from src.server.services import hook_outbox
    from src.server.services.report_back import notify_turn, steer_executor

    state_file = tmp_path / "maintenance-admission.json"
    monkeypatch.setenv("DALTON_MAINTENANCE_ADMISSION_FILE", str(state_file))
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "svc-tok-r4-steer")

    payload = {
        "target": "thread",
        "sender": "user",
        "content": "check guidance",
        "user_id": "u-st-1",
        "workspace_id": "ws-st-1",
        "platform": "web",
        "chat_app": False,
        "request_key": "rk-st-503",
    }
    row = {
        "id": "job-st-503",
        "status": "claimed",
        "attempts": 1,
        "run_id": "run-prev",
        "payload": payload,
    }
    reschedule_mock = create_autospec(outbox_db.reschedule_claimed_job, return_value="pending")
    nack_mock = create_autospec(outbox_db.nack_outbox_job, return_value="pending")
    ack_mock = create_autospec(outbox_db.ack_outbox_job, return_value=True)
    finish_mock = create_autospec(outbox_db.finish_claimed_steer, return_value=True)
    settle_mock = create_autospec(
        steer_executor._settle_dispatched,
        side_effect=AssertionError("_settle_dispatched must not be called on maintenance 503"),
    )

    monkeypatch.setattr(outbox_db, "reschedule_claimed_job", reschedule_mock)
    monkeypatch.setattr(outbox_db, "nack_outbox_job", nack_mock)
    monkeypatch.setattr(outbox_db, "ack_outbox_job", ack_mock)
    monkeypatch.setattr(outbox_db, "finish_claimed_steer", finish_mock)
    monkeypatch.setattr(outbox_db, "get_steer_job", create_autospec(outbox_db.get_steer_job, return_value=row))
    monkeypatch.setattr(outbox_db, "extend_job_lease", create_autospec(outbox_db.extend_job_lease, return_value=True))
    monkeypatch.setattr(tl_db, "get_latest_attempt", create_autospec(tl_db.get_latest_attempt, return_value={"status": "completed"}))
    monkeypatch.setattr(notify_turn, "park_unless_ready", create_autospec(notify_turn.park_unless_ready, return_value=False))
    monkeypatch.setattr(steer_executor, "_settle_dispatched", settle_mock)

    post_counter: list[int] = []
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        _make_autospec_503_maintenance_session_factory(post_counter),
    )
    monkeypatch.setitem(hook_outbox._EXECUTORS, "steer", steer_executor.execute_steer)

    job = {
        "hook_outbox_id": "job-st-503",
        "hook_type": "steer",
        "idempotency_key": "steer:st-503",
        "conversation_thread_id": "thr-st-1",
        "attempts": 1,
        "payload": payload,
    }
    drainer = hook_outbox.HookOutboxDrainer()
    await drainer._execute(job)

    assert len(post_counter) == 1
    assert reschedule_mock.await_count == 1
    assert nack_mock.await_count == 0
    assert ack_mock.await_count == 0
    assert finish_mock.await_count == 0
    assert settle_mock.await_count == 0


# ---------------------------------------------------------------------------
# r5 Review Fixes: proxy normal EOF > queue capacity, real connect/TLS timeout
# & worker-gated in_flight, host-direct allowlist fail-closed, sales-mail
# real gateway list/read contract, task_steer effective_attempts backoff
# ---------------------------------------------------------------------------


def _loopback_tcp_connect_allowed() -> bool:
    """Return True when 127.0.0.1 TCP connect is permitted (False inside network-isolated sandboxes)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        cli.settimeout(0.5)
        cli.connect(("127.0.0.1", port))
        return True
    except PermissionError:
        return False
    except OSError:
        return False
    finally:
        for s in (cli, srv):
            try:
                s.close()
            except OSError:
                pass


@pytest.mark.enable_socket
def test_r5_item1_proxy_8943_normal_response_larger_than_queue_completes_before_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r5 Item 1: upstream response larger than chunk_queue capacity finishes immediately on normal EOF without stalling to deadline."""
    import http.server
    import threading

    _clean_cap_env(monkeypatch)
    gp_lock_mod = _load_script_module(
        "_test_r5_gp_lock_item1", RESEARCH_RUNTIME_DIR / "guidepoint_lock.py"
    )
    proxy_mod = _load_script_module(
        "_test_r5_gp_proxy_item1", RESEARCH_RUNTIME_DIR / "scripts" / "guidepoint_mcp_proxy.py"
    )

    journal_file = tmp_path / "handoff-journal.json"
    token_file = tmp_path / "config" / "guidepoint_mcp_tokens.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(
        json.dumps(
            {
                "access_token": "tok-r5-eof",
                "refresh_token": "ref-r5-eof",
                "obtained_at": time.time(),
                "expires_in": 600,
            }
        ),
        encoding="utf-8",
    )
    gp_lock_mod.set_target_owner(gp_lock_mod.OWNER_LEGACY_PROXY, ownership_file=journal_file)
    monkeypatch.setenv("DALTON_GUIDEPOINT_OWNERSHIP_FILE", str(journal_file))
    monkeypatch.setenv("GUIDEPOINT_TOKENS_FILE", str(token_file))

    # 80 KiB payload (> 20 x 4 KiB chunks, 5x the 4-slot queue capacity)
    big_payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "G" * 81920}]},
        }
    ).encode("utf-8")

    class _UpstreamHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(big_payload)))
            self.end_headers()
            self.wfile.write(big_payload)
            self.wfile.flush()

        def log_message(self, fmt: str, *args: Any) -> None:
            _ = (fmt, args)

    use_tcp = _loopback_tcp_connect_allowed()
    upstream_srv = None
    if use_tcp:
        upstream_srv = proxy_mod.ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
        up_port = upstream_srv.server_address[1]
        threading.Thread(target=upstream_srv.serve_forever, daemon=True).start()
    else:
        # Sandbox blocks AF_INET connect(): subclass socket.socket so _TrackedHTTPConnection's
        # socket connects over a real OS socketpair whose server end runs _UpstreamHandler
        # (still exercises the real urllib opener, timeouts, and _close_resource_forcefully!).
        up_port = 58943
        orig_socket_cls = proxy_mod.socket.socket

        class _PairBridgedSocket(orig_socket_cls):
            def __init__(
                self,
                family: int = socket.AF_INET,
                stype: int = socket.SOCK_STREAM,
                proto: int = 0,
                fileno: Any = None,
            ) -> None:
                if fileno is None and family == socket.AF_INET and stype == socket.SOCK_STREAM:
                    cli_pair, srv_pair = socket.socketpair()
                    self._srv_pair: socket.socket | None = srv_pair
                    orig_socket_cls.__init__(self, fileno=cli_pair.detach())
                else:
                    self._srv_pair = None
                    orig_socket_cls.__init__(self, family, stype, proto, fileno)

            def connect(self, address: Any) -> None:
                if self._srv_pair is not None:
                    srv_pair = self._srv_pair
                    threading.Thread(
                        target=lambda: _UpstreamHandler(srv_pair, ("127.0.0.1", 0), None),
                        daemon=True,
                    ).start()
                    return
                super().connect(address)

        monkeypatch.setattr(proxy_mod.socket, "socket", _PairBridgedSocket)

    monkeypatch.setattr(proxy_mod, "UPSTREAM", f"http://127.0.0.1:{up_port}/mcp")
    proxy_mod.initialize_runtime(ownership_file=journal_file, token_store=token_file)
    try:
        req_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "search_library",
                    "arguments": {"query": "cloud", "size": 5, "timeout": 10},
                },
            }
        ).encode("utf-8")
        raw_http_req = (
            b"POST /mcp HTTP/1.1\r\n"
            b"Host: 127.0.0.1:8943\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(req_body)}\r\n\r\n".encode("ascii")
            + req_body
        )

        cli, srv = socket.socketpair()
        cli.settimeout(5.0)
        cli.sendall(raw_http_req)
        cli.shutdown(socket.SHUT_WR)
        resp_box: dict[str, Any] = {}

        def _client_reader() -> None:
            # Brief initial pause lets upstream fill all 4 queue slots and hit EOF before consumer drains
            time.sleep(0.08)
            try:
                resp = http.client.HTTPResponse(cli)
                resp.begin()
                resp_box["status"] = resp.status
                resp_box["te"] = resp.getheader("Transfer-Encoding")
                resp_box["body"] = resp.read()
            except Exception as exc:
                resp_box["error"] = exc
            finally:
                cli.close()

        reader = threading.Thread(target=_client_reader, daemon=True)
        reader.start()
        t0 = time.monotonic()
        try:
            proxy_mod.ProxyHandler(srv, ("127.0.0.1", 0), None)
        finally:
            try:
                srv.close()
            except OSError:
                pass
        elapsed = time.monotonic() - t0
        reader.join(timeout=3.0)

        assert elapsed < 2.0, f"Normal >64KiB response stalled toward 10s deadline: {elapsed:.2f}s"
        assert "error" not in resp_box, f"Unexpected client read error: {resp_box.get('error')!r}"
        assert resp_box.get("status") == 200, f"Unexpected status: {resp_box!r}"
        assert str(resp_box.get("te") or "").lower() == "chunked"
        assert resp_box.get("body") == big_payload
        deadline_wait = time.monotonic() + 1.0
        while proxy_mod.health_payload()["in_flight"] != 0 and time.monotonic() < deadline_wait:
            time.sleep(0.01)
        assert proxy_mod.health_payload()["in_flight"] == 0
    finally:
        if upstream_srv is not None:
            upstream_srv.shutdown()
            upstream_srv.server_close()
        proxy_mod.release_owner_lock()


@pytest.mark.enable_socket
def test_r5_item2_proxy_8943_slow_connect_and_delayed_tls_handshake_reap_thread_and_zero_inflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r5 Item 2: real opener enforces min(timeout, remaining) during connect & TLS handshake, terminates worker & socket at deadline, and decrements in_flight only after worker exits."""
    import threading

    _clean_cap_env(monkeypatch)
    gp_lock_mod = _load_script_module(
        "_test_r5_gp_lock_item2", RESEARCH_RUNTIME_DIR / "guidepoint_lock.py"
    )
    proxy_mod = _load_script_module(
        "_test_r5_gp_proxy_item2", RESEARCH_RUNTIME_DIR / "scripts" / "guidepoint_mcp_proxy.py"
    )

    journal_file = tmp_path / "handoff-journal.json"
    token_file = tmp_path / "config" / "guidepoint_mcp_tokens.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(
        json.dumps(
            {
                "access_token": "tok-r5-hs",
                "refresh_token": "ref-r5-hs",
                "obtained_at": time.time(),
                "expires_in": 600,
            }
        ),
        encoding="utf-8",
    )
    gp_lock_mod.set_target_owner(gp_lock_mod.OWNER_LEGACY_PROXY, ownership_file=journal_file)
    monkeypatch.setenv("DALTON_GUIDEPOINT_OWNERSHIP_FILE", str(journal_file))
    monkeypatch.setenv("GUIDEPOINT_TOKENS_FILE", str(token_file))

    req_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search_library",
                "arguments": {"query": "semis", "size": 1, "timeout": 0.5},
            },
        }
    ).encode("utf-8")
    raw_http_req = (
        b"POST /mcp HTTP/1.1\r\n"
        b"Host: 127.0.0.1:8943\r\n"
        b"X-Dalton-Request-Timeout-Ms: 500\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(req_body)}\r\n\r\n".encode("ascii")
        + req_body
    )

    # Track upstream worker threads spawned by ProxyHandler without altering production opener
    spawned_workers: list[threading.Thread] = []
    orig_thread_cls = proxy_mod.threading.Thread

    class _ObservingThread(orig_thread_cls):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            target = kwargs.get("target") or (args[1] if len(args) > 1 else None)
            if getattr(target, "__name__", "") == "_upstream_worker":
                spawned_workers.append(self)

    monkeypatch.setattr(proxy_mod.threading, "Thread", _ObservingThread)
    proxy_mod.initialize_runtime(ownership_file=journal_file, token_store=token_file)
    use_tcp = _loopback_tcp_connect_allowed()
    orig_socket_cls = proxy_mod.socket.socket
    try:
        # Case A: Delayed TLS handshake on real HTTPS opener (NO urlopen mock!)
        peer_accepted = threading.Event()
        peer_closed = threading.Event()
        stop_tls = threading.Event()
        tls_listener = None
        tls_srv_thread = None

        def _hold_silent_tls_peer(conn: socket.socket) -> None:
            try:
                peer_accepted.set()
                conn.settimeout(3.0)
                while not stop_tls.is_set():
                    try:
                        data = conn.recv(4096)
                        if not data:
                            peer_closed.set()
                            break
                    except (TimeoutError, socket.timeout):
                        continue
                    except OSError:
                        peer_closed.set()
                        break
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

        if use_tcp:
            tls_listener = orig_socket_cls(socket.AF_INET, socket.SOCK_STREAM)
            tls_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            tls_listener.bind(("127.0.0.1", 0))
            tls_listener.listen(4)
            tls_listener.settimeout(4.0)
            tls_port = tls_listener.getsockname()[1]

            def _accept_and_stall() -> None:
                try:
                    conn, _addr = tls_listener.accept()
                except OSError:
                    return
                _hold_silent_tls_peer(conn)

            tls_srv_thread = orig_thread_cls(target=_accept_and_stall, daemon=True)
            tls_srv_thread.start()
        else:
            tls_port = 58944

            class _TlsPairBridgedSocket(orig_socket_cls):
                def __init__(
                    self,
                    family: int = socket.AF_INET,
                    stype: int = socket.SOCK_STREAM,
                    proto: int = 0,
                    fileno: Any = None,
                ) -> None:
                    if fileno is None and family == socket.AF_INET and stype == socket.SOCK_STREAM:
                        cli_pair, srv_pair = socket.socketpair()
                        self._srv_pair: socket.socket | None = srv_pair
                        orig_socket_cls.__init__(self, fileno=cli_pair.detach())
                    else:
                        self._srv_pair = None
                        orig_socket_cls.__init__(self, family, stype, proto, fileno)

                def connect(self, address: Any) -> None:
                    if self._srv_pair is not None:
                        srv_pair = self._srv_pair
                        orig_thread_cls(
                            target=_hold_silent_tls_peer, args=(srv_pair,), daemon=True
                        ).start()
                        return
                    super().connect(address)

            monkeypatch.setattr(proxy_mod.socket, "socket", _TlsPairBridgedSocket)

        monkeypatch.setattr(proxy_mod, "UPSTREAM", f"https://127.0.0.1:{tls_port}/mcp")

        cli_a, srv_a = socket.socketpair()
        cli_a.settimeout(4.0)
        cli_a.sendall(raw_http_req)
        cli_a.shutdown(socket.SHUT_WR)
        rec_a = bytearray()

        def _drain_a() -> None:
            try:
                while True:
                    part = cli_a.recv(4096)
                    if not part:
                        break
                    rec_a.extend(part)
            finally:
                cli_a.close()

        ta = orig_thread_cls(target=_drain_a, daemon=True)
        ta.start()
        t0_a = time.monotonic()
        try:
            proxy_mod.ProxyHandler(srv_a, ("127.0.0.1", 0), None)
        finally:
            try:
                srv_a.close()
            except OSError:
                pass
        elapsed_a = time.monotonic() - t0_a
        ta.join(timeout=2.0)

        assert peer_accepted.wait(timeout=1.0), "Proxy never connected to TLS peer"
        assert elapsed_a < 2.0, f"Delayed TLS handshake exceeded 0.5s deadline: {elapsed_a:.2f}s"
        assert b"504" in bytes(rec_a)
        assert b"upstream_timeout" in bytes(rec_a)
        assert len(spawned_workers) == 1
        spawned_workers[0].join(timeout=2.0)
        assert not spawned_workers[0].is_alive(), "Upstream worker thread survived past TLS handshake deadline"
        assert peer_closed.wait(timeout=2.0), "Upstream socket was not closed at TLS handshake deadline"
        assert proxy_mod.health_payload()["in_flight"] == 0
        stop_tls.set()
        if tls_listener is not None:
            tls_listener.close()
        if tls_srv_thread is not None:
            tls_srv_thread.join(timeout=1.0)

        # Case B: Non-accepting local listener + slow TCP connect timeout, verifying socket timeout
        # uses min(orig, remaining) and in_flight stays >= 1 until the upstream worker truly exits.
        no_accept_sock = orig_socket_cls(socket.AF_INET, socket.SOCK_STREAM)
        no_accept_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        no_accept_sock.bind(("127.0.0.1", 0))
        no_accept_sock.listen(1)
        no_accept_port = no_accept_sock.getsockname()[1]

        recorded_connect_timeouts: list[float] = []
        worker_hold = threading.Event()
        worker_exiting = threading.Event()

        class _SlowConnectSocket(orig_socket_cls):
            def connect(self, address: Any) -> None:
                if isinstance(address, tuple) and len(address) >= 2 and address[1] == no_accept_port:
                    t_val = self.gettimeout()
                    assert isinstance(t_val, float) and 0.0 < t_val <= 0.6
                    recorded_connect_timeouts.append(t_val)
                    time.sleep(t_val)
                    worker_exiting.set()
                    worker_hold.wait(timeout=2.0)
                    raise TimeoutError("simulated slow connect timeout")
                return super().connect(address)

        monkeypatch.setattr(proxy_mod.socket, "socket", _SlowConnectSocket)
        monkeypatch.setattr(proxy_mod, "UPSTREAM", f"http://127.0.0.1:{no_accept_port}/mcp")

        cli_b, srv_b = socket.socketpair()
        cli_b.settimeout(4.0)
        cli_b.sendall(raw_http_req)
        cli_b.shutdown(socket.SHUT_WR)
        rec_b = bytearray()

        def _drain_b() -> None:
            try:
                while True:
                    part = cli_b.recv(4096)
                    if not part:
                        break
                    rec_b.extend(part)
            finally:
                cli_b.close()

        tb = orig_thread_cls(target=_drain_b, daemon=True)
        tb.start()
        t0_b = time.monotonic()
        try:
            proxy_mod.ProxyHandler(srv_b, ("127.0.0.1", 0), None)
        finally:
            try:
                srv_b.close()
            except OSError:
                pass
        elapsed_b = time.monotonic() - t0_b
        tb.join(timeout=2.0)

        assert elapsed_b < 2.0, f"Slow connect exceeded 0.5s deadline: {elapsed_b:.2f}s"
        assert b"504" in bytes(rec_b)
        assert recorded_connect_timeouts and recorded_connect_timeouts[0] <= 0.55
        assert len(spawned_workers) == 2
        worker_b = spawned_workers[1]
        # Main handler already returned 504, while worker_b is still blocked before releasing:
        # in_flight MUST stay 1 until worker_b actually exits its cleanup callback!
        assert worker_exiting.wait(timeout=1.5)
        assert worker_b.is_alive()
        assert proxy_mod.health_payload()["in_flight"] == 1
        worker_hold.set()
        worker_b.join(timeout=2.0)
        assert not worker_b.is_alive()
        assert proxy_mod.health_payload()["in_flight"] == 0
        no_accept_sock.close()
    finally:
        proxy_mod.release_owner_lock()


def test_r5_item3_host_direct_resolves_deployment_allowlist_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r5 Item 3: host-direct data_gateway/research_library resolve deployment allowlist (names only, no .env secrets into child env), reject disabled sales-notes, and fail closed when config is unresolvable."""
    import sqlite3

    _clean_cap_env(monkeypatch)
    shared_env = tmp_path / "shared.env"
    shared_env.write_text(
        "\n".join(
            [
                "APP_ENV=dalton",
                "DALTON_PROFILE=everflow",
                "DALTON_CAP_SALES_NOTES=false",
                "SUPABASE_SECRET_KEY=r5-supa-secret-no-leak",
                "DB_PASSWORD=r5-db-pass-no-leak",
                "POSTGRES_PASSWORD=r5-pg-pass-no-leak",
                "DALTON_RESEARCH_ADMIN_TOKEN=r5-admin-tok-no-leak",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    dg_script = RESEARCH_RUNTIME_DIR / "scripts" / "data_gateway.py"
    rl_script = RESEARCH_RUNTIME_DIR / "scripts" / "research_library.py"

    base_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "DALTON_DOTENV_PATH": str(shared_env),
    }

    # 1. Host-direct data_gateway plan for disabled sales-notes -> REJECTED
    res_plan_sn = subprocess.run(
        [sys.executable, str(dg_script), "plan", "sales-notes", "--operation=search", "--query=NVDA"],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_plan_sn.returncode != 0
    out_sn = json.loads(res_plan_sn.stdout)
    assert out_sn["ok"] is False
    assert "capability_disabled" in str(out_sn.get("error") or "")

    # 2. Host-direct research_library.py for disabled sales-notes -> REJECTED
    res_rl_sn = subprocess.run(
        [sys.executable, str(rl_script), "sales-notes", "--operation=list"],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_rl_sn.returncode != 0
    out_rl_sn = json.loads(res_rl_sn.stdout)
    assert out_rl_sn["ok"] is False
    assert "capability_disabled" in str(out_rl_sn.get("error") or "")

    # 3. Host-direct data_gateway plan for enabled company-wiki -> passes ONLY source names via --allowed-sources
    res_plan_wiki = subprocess.run(
        [sys.executable, str(dg_script), "plan", "company-wiki", "--operation=list"],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_plan_wiki.returncode == 0, res_plan_wiki.stdout + res_plan_wiki.stderr
    out_wiki = json.loads(res_plan_wiki.stdout)
    assert out_wiki["ok"] is True
    assert "--allowed-sources=company-wiki,tmtb,funda-ai" in out_wiki["argv"]

    # 4. Host-direct data_gateway fetch executes real research_library.sh child and NEVER leaks .env secrets into child env
    rt_code = tmp_path / "rt_code"
    rt_home = tmp_path / "rt_home"
    (rt_code / "scripts").mkdir(parents=True)
    (rt_code / "config").mkdir(parents=True)
    (rt_code / "skills" / "company-wiki" / "scripts").mkdir(parents=True)
    (rt_home / "wiki").mkdir(parents=True)
    venv_bin = rt_home / "venvs" / "market-digest" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(Path(sys.executable).resolve())
    for fname in ("research_library.py", "research_library.sh", "research_images.py"):
        dst = rt_code / "scripts" / fname
        shutil.copy2(RESEARCH_RUNTIME_DIR / "scripts" / fname, dst)
        if fname.endswith(".sh"):
            dst.chmod(0o755)
    shutil.copy2(
        RESEARCH_RUNTIME_DIR / "config" / "data_sources.json",
        rt_code / "config" / "data_sources.json",
    )
    env_dump_path = tmp_path / "host_direct_child_env.json"
    (rt_code / "skills" / "company-wiki" / "config.json").write_text("{}", encoding="utf-8")
    (rt_code / "skills" / "company-wiki" / "scripts" / "company_wiki_cli.py").write_text(
        f"""
import json, os, sys
from pathlib import Path

def build_doc_filters(_ct, _cn, _start, _end, _tag):
    Path({str(env_dump_path)!r}).write_text(
        json.dumps(
            {{
                "env": dict(os.environ),
                "loaded_src_config": "src.config" in sys.modules,
            }},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return [], []
""".lstrip(),
        encoding="utf-8",
    )
    with sqlite3.connect(rt_home / "wiki" / "vectors.db") as conn:
        conn.execute(
            "CREATE TABLE documents ("
            "id INTEGER PRIMARY KEY, filepath TEXT, filename TEXT, date TEXT, "
            "created_at TEXT, category_type TEXT, category_name TEXT, tags TEXT)"
        )
        conn.commit()

    res_fetch_wiki = subprocess.run(
        [
            sys.executable,
            str(dg_script),
            "fetch",
            "company-wiki",
            "--operation=list",
            "--params-json={\"media\":\"text\"}",
        ],
        env={
            **base_env,
            "DALTON_RESEARCH_CODE": str(rt_code),
            "DALTON_RESEARCH_HOME": str(rt_home),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_fetch_wiki.returncode == 0, res_fetch_wiki.stdout + res_fetch_wiki.stderr
    assert env_dump_path.is_file()
    dumped = json.loads(env_dump_path.read_text(encoding="utf-8"))
    assert dumped["loaded_src_config"] is False
    for secret_key in (
        "SUPABASE_SECRET_KEY",
        "DB_PASSWORD",
        "POSTGRES_PASSWORD",
        "DALTON_RESEARCH_ADMIN_TOKEN",
    ):
        assert secret_key not in dumped["env"], f"Host-direct leaked {secret_key} into child env"

    # 5. Unresolvable deployment config fails closed (refuses instead of allowing all)
    # 5a. Conflicting profile env
    res_conflict = subprocess.run(
        [sys.executable, str(dg_script), "plan", "company-wiki", "--operation=list"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "APP_ENV": "dalton",
            "DALTON_PROFILE": "core",
            "PYTHON_DOTENV_DISABLED": "1",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_conflict.returncode != 0
    assert "capability_disabled" in str(json.loads(res_conflict.stdout).get("error") or "")

    # 5b. Isolated research_library.py without repo src/dalton ancestor -> fails closed
    res_isolated = subprocess.run(
        [sys.executable, str(rt_code / "scripts" / "research_library.py"), "company-wiki", "--operation=list"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "DALTON_RESEARCH_CODE": str(rt_code),
            "DALTON_RESEARCH_HOME": str(rt_home),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_isolated.returncode != 0
    assert "capability_disabled" in str(json.loads(res_isolated.stdout).get("error") or "")


@pytest.mark.asyncio
async def test_r5_item4_sales_mail_list_and_read_with_sources_via_real_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r5 Item 4: sales-mail list and read with legal single source (e.g. ['sales-notes'], ['tmtb']) succeed through real data_gateway + research_library + bridge /mcp."""
    import sqlite3

    _clean_cap_env(monkeypatch)
    bridge_mod = _load_script_module(
        "_test_r5_bridge_item4", SCRIPTS_DALTON_DIR / "researchdata_bridge.py"
    )

    rt_code = tmp_path / "rt_code"
    rt_home = tmp_path / "rt_home"
    (rt_code / "scripts").mkdir(parents=True)
    (rt_code / "config").mkdir(parents=True)
    (rt_home / "skills" / "market-digest").mkdir(parents=True)
    venv_bin = rt_home / "venvs" / "market-digest" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(Path(sys.executable).resolve())

    for fname in (
        "researchdata_mcp.py",
        "data_gateway.py",
        "research_library.py",
        "research_library.sh",
        "research_images.py",
    ):
        dst = rt_code / "scripts" / fname
        shutil.copy2(RESEARCH_RUNTIME_DIR / "scripts" / fname, dst)
        if fname.endswith(".sh"):
            dst.chmod(0o755)
    shutil.copy2(
        RESEARCH_RUNTIME_DIR / "config" / "data_sources.json",
        rt_code / "config" / "data_sources.json",
    )

    mail_db = rt_home / "skills" / "market-digest" / "vectors.db"
    with sqlite3.connect(mail_db) as conn:
        conn.execute(
            "CREATE TABLE email_chunks ("
            "id INTEGER PRIMARY KEY, date TEXT, email_id TEXT, message_id TEXT, "
            "sender TEXT, subject TEXT, chunk_index INTEGER, content TEXT, "
            "created_at TEXT, embedding BLOB)"
        )
        conn.execute(
            "CREATE TABLE chunks ("
            "id INTEGER PRIMARY KEY, date TEXT, period TEXT, section_id TEXT, "
            "section_label TEXT, content TEXT, source_emails TEXT, "
            "created_at TEXT, embedding BLOB)"
        )
        conn.execute(
            "INSERT INTO email_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                101,
                "2026-11-20",
                "em-101",
                "msg-101",
                "Goldman Sales",
                "NVDA Blackwell Checks",
                0,
                "NVDA Blackwell rack shipments accelerating into Q1.",
                "2026-11-20T08:00:00Z",
                None,
            ),
        )
        conn.execute(
            "INSERT INTO email_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                102,
                "2026-11-21",
                "em-102",
                "alphaengine:tmtb_doc_9",
                "TMT Breakout",
                "TMTB Semicap Note",
                0,
                "TMTB semicap order visibility improving across memory.",
                "2026-11-21T08:00:00Z",
                None,
            ),
        )
        conn.commit()

    repo_env = tmp_path / "repo.env"
    repo_env.write_text(
        "APP_ENV=dalton\nDALTON_PROFILE=everflow\nDALTON_CAP_SALES_NOTES=true\nDALTON_CAP_TMTB=true\n",
        encoding="utf-8",
    )
    bridge_env = tmp_path / "bridge.env"
    bridge_env.write_text("DALTON_BRIDGE_TOKEN=r5-sm-tok\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_DOTENV_PATH", str(repo_env))
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))
    monkeypatch.setenv("DALTON_RESEARCH_CODE", str(rt_code))
    monkeypatch.setenv("DALTON_RESEARCH_HOME", str(rt_home))

    gw_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "DALTON_DOTENV_PATH": str(repo_env),
        "DALTON_RESEARCH_CODE": str(rt_code),
        "DALTON_RESEARCH_HOME": str(rt_home),
        "DALTON_ENABLED_LIBRARY_SOURCES": "company-wiki,sales-notes,tmtb,funda-ai",
    }
    dg_script = rt_code / "scripts" / "data_gateway.py"

    # 1. Real data_gateway plan + fetch for sales-mail list
    plan_list = subprocess.run(
        [sys.executable, str(dg_script), "plan", "sales-mail", "--operation=list"],
        env=gw_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert plan_list.returncode == 0, plan_list.stdout + plan_list.stderr
    assert json.loads(plan_list.stdout)["ok"] is True

    fetch_list = subprocess.run(
        [
            sys.executable,
            str(dg_script),
            "fetch",
            "sales-mail",
            "--operation=list",
            '--params-json={"sources":["tmtb"],"media":"text"}',
        ],
        env=gw_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert fetch_list.returncode == 0, fetch_list.stdout + fetch_list.stderr
    list_out = json.loads(fetch_list.stdout)
    assert list_out["ok"] is True
    assert list_out["data"]["results"]
    assert all(r["source"] == "tmtb" for r in list_out["data"]["results"])

    # 2. Real data_gateway plan + fetch for sales-mail read with legal single source ["sales-notes"]
    plan_read = subprocess.run(
        [
            sys.executable,
            str(dg_script),
            "plan",
            "sales-mail",
            "--operation=read",
            "--document=mail:101",
            '--params-json={"sources":["sales-notes"]}',
        ],
        env=gw_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert plan_read.returncode == 0, plan_read.stdout + plan_read.stderr
    assert json.loads(plan_read.stdout)["ok"] is True

    fetch_read = subprocess.run(
        [
            sys.executable,
            str(dg_script),
            "fetch",
            "sales-mail",
            "--operation=read",
            "--document=mail:101",
            '--params-json={"sources":["sales-notes"]}',
        ],
        env=gw_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert fetch_read.returncode == 0, fetch_read.stdout + fetch_read.stderr
    read_out = json.loads(fetch_read.stdout)
    assert read_out["ok"] is True
    assert "NVDA Blackwell rack shipments" in read_out["data"]["document"]["content"]

    # 3. End-to-end real bridge /mcp -> researchdata_mcp -> data_gateway -> research_library (NO _run_stdio_rpc mock!)
    cfg = bridge_mod.BridgeConfig()
    cfg.python_bin = sys.executable
    cfg.mcp_script = rt_code / "scripts" / "researchdata_mcp.py"
    cfg.research_code = rt_code
    cfg.research_home = rt_home
    bridge = bridge_mod.ResearchDataBridge(cfg)

    code_list, body_list = await _send_real_bridge_mcp_request(
        bridge_mod,
        bridge,
        token="r5-sm-tok",
        rpc_body={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "research",
                "arguments": {
                    "route": "sales-mail",
                    "operation": "list",
                    "params": {"sources": ["tmtb"], "media": "text"},
                },
            },
        },
    )
    assert code_list == 200
    assert body_list["result"]["isError"] is False
    list_mcp_payload = json.loads(body_list["result"]["content"][0]["text"])
    assert list_mcp_payload["ok"] is True
    assert any(r["document_id"] == "tmtb:tmtb_doc_9" for r in list_mcp_payload["data"]["results"])

    code_read_sn, body_read_sn = await _send_real_bridge_mcp_request(
        bridge_mod,
        bridge,
        token="r5-sm-tok",
        rpc_body={
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "research",
                "arguments": {
                    "route": "sales-mail",
                    "operation": "read",
                    "document": "mail:101",
                    "params": {"sources": ["sales-notes"]},
                },
            },
        },
    )
    assert code_read_sn == 200
    assert body_read_sn["result"]["isError"] is False
    read_sn_payload = json.loads(body_read_sn["result"]["content"][0]["text"])
    assert read_sn_payload["ok"] is True
    assert "NVDA Blackwell rack shipments" in read_sn_payload["data"]["document"]["content"]

    code_read_tmtb, body_read_tmtb = await _send_real_bridge_mcp_request(
        bridge_mod,
        bridge,
        token="r5-sm-tok",
        rpc_body={
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "research",
                "arguments": {
                    "route": "sales-mail",
                    "operation": "read",
                    "document": "tmtb:tmtb_doc_9",
                    "params": {"sources": ["tmtb"]},
                },
            },
        },
    )
    assert code_read_tmtb == 200
    assert body_read_tmtb["result"]["isError"] is False
    read_tmtb_payload = json.loads(body_read_tmtb["result"]["content"][0]["text"])
    assert read_tmtb_payload["ok"] is True
    assert "TMTB semicap order visibility" in read_tmtb_payload["data"]["document"]["content"]


@pytest.mark.asyncio
async def test_r5_item5_task_steer_full_computer_backoff_uses_effective_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r5 Item 5: after 6 maintenance reschedules, first full-computer repark computes backoff from effective_attempts() (1x _ROOM_WAIT_S, not 64x)."""
    from src.server.database.runs import outbox as outbox_db
    from src.server.services.report_back import notify_turn, task_steer

    orig_get_steer_job = outbox_db.get_steer_job
    orig_defer_claimed_job = outbox_db.defer_claimed_job
    orig_divert = task_steer._divert
    orig_turn_owner = task_steer.turn_owner
    orig_computer_full = task_steer._computer_full
    orig_park_unless_ready = notify_turn.park_unless_ready
    orig_post_notification_turn = notify_turn.post_notification_turn

    async def _run_case(*, attempts: int, reschedules: int, waited: int = 0) -> int:
        payload: dict[str, Any] = {
            "target": "task",
            "task_id": "k7Xm2p",
            "content": "continue after maintenance",
            "user_id": "u-r5-ts",
            "workspace_id": "ws-r5-ts",
            "reschedules": reschedules,
        }
        if waited:
            payload["waited"] = waited
        row = {
            "id": "job-r5-ts-full",
            "status": "claimed",
            "attempts": attempts,
            "payload": payload,
        }
        defer_mock = create_autospec(orig_defer_claimed_job, return_value="pending")
        post_mock = create_autospec(orig_post_notification_turn, return_value=("posted", "run-1"))

        monkeypatch.setattr(outbox_db, "get_steer_job", create_autospec(orig_get_steer_job, return_value=row))
        monkeypatch.setattr(outbox_db, "defer_claimed_job", defer_mock)
        monkeypatch.setattr(task_steer, "_divert", create_autospec(orig_divert, return_value=False))
        monkeypatch.setattr(task_steer, "turn_owner", create_autospec(orig_turn_owner, return_value=("u-r5-ts", "ws-r5-ts")))
        monkeypatch.setattr(task_steer, "_computer_full", create_autospec(orig_computer_full, return_value=True))
        monkeypatch.setattr(notify_turn, "park_unless_ready", create_autospec(orig_park_unless_ready, return_value=False))
        monkeypatch.setattr(notify_turn, "post_notification_turn", post_mock)

        job = {
            "hook_outbox_id": "job-r5-ts-full",
            "hook_type": "steer",
            "idempotency_key": "steer:r5-ts-full",
            "ordering_key": "thr-r5-ts",
            "conversation_thread_id": "thr-r5-ts",
            "attempts": attempts,
            "payload": payload,
        }
        await task_steer.execute_task_steer(job)
        assert post_mock.await_count == 0
        assert defer_mock.await_count == 1
        return int(defer_mock.await_args.kwargs["wait_s"])

    # 6 maintenance reschedules, then 1st full-computer encounter (claim #7):
    # effective_attempts(7, {reschedules: 6}) == 1 -> 2**0 * _ROOM_WAIT_S (NOT 2**6 * _ROOM_WAIT_S)
    assert await _run_case(attempts=7, reschedules=6) == task_steer._ROOM_WAIT_S
    # Next full-computer encounter (claim #8): effective_attempts == 2 -> 2 * _ROOM_WAIT_S
    assert await _run_case(attempts=8, reschedules=6) == 2 * task_steer._ROOM_WAIT_S
    # Combined maintenance reschedules (6) + waited (2) at claim #9: effective_attempts == 1 -> 1 * _ROOM_WAIT_S
    assert await _run_case(attempts=9, reschedules=6, waited=2) == task_steer._ROOM_WAIT_S


@pytest.mark.enable_socket
def test_r6_item1_proxy_8943_upstream_midstream_disconnect_fails_client_not_complete_200(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6 Item 1: upstream mid-stream disconnect (truncated Content-Length, unfinished chunked, or immediate drop) surfaces an error (IncompleteRead or 502) to the HTTP/1.1 client rather than a complete 200."""
    import http.client
    import threading

    _clean_cap_env(monkeypatch)
    gp_lock_mod = _load_script_module(
        "_test_r6_gp_lock_item1", RESEARCH_RUNTIME_DIR / "guidepoint_lock.py"
    )
    proxy_mod = _load_script_module(
        "_test_r6_gp_proxy_item1", RESEARCH_RUNTIME_DIR / "scripts" / "guidepoint_mcp_proxy.py"
    )

    journal_file = tmp_path / "handoff-journal.json"
    token_file = tmp_path / "config" / "guidepoint_mcp_tokens.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(
        json.dumps(
            {
                "access_token": "tok-r6-trunc",
                "refresh_token": "ref-r6-trunc",
                "obtained_at": time.time(),
                "expires_in": 600,
            }
        ),
        encoding="utf-8",
    )
    gp_lock_mod.set_target_owner(gp_lock_mod.OWNER_LEGACY_PROXY, ownership_file=journal_file)
    monkeypatch.setenv("DALTON_GUIDEPOINT_OWNERSHIP_FILE", str(journal_file))
    monkeypatch.setenv("GUIDEPOINT_TOKENS_FILE", str(token_file))

    req_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_library", "arguments": {"query": "cloud", "timeout": 5}},
        }
    ).encode("utf-8")
    raw_http_req = (
        b"POST /mcp HTTP/1.1\r\n"
        b"Host: 127.0.0.1:8943\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(req_body)}\r\n\r\n".encode("ascii")
        + req_body
    )

    orig_urlopen = proxy_mod.urllib.request.urlopen
    proxy_mod.initialize_runtime(ownership_file=journal_file, token_store=token_file)
    try:
        def _run_disconnect_scenario(raw_chunks: list[tuple[float, bytes]]) -> dict[str, Any]:
            def _fake_urlopen(
                url: Any,
                data: Any = None,
                timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
                *,
                context: Any = None,
            ) -> Any:
                _ = (url, data, context)
                up_cli, up_srv = socket.socketpair()
                up_cli.settimeout(float(timeout) if isinstance(timeout, (int, float)) else 5.0)

                def _serve_broken() -> None:
                    try:
                        for delay_s, piece in raw_chunks:
                            if delay_s > 0:
                                time.sleep(delay_s)
                            up_srv.sendall(piece)
                    except OSError:
                        pass
                    finally:
                        try:
                            up_srv.close()
                        except OSError:
                            pass

                threading.Thread(target=_serve_broken, daemon=True).start()
                resp = http.client.HTTPResponse(up_cli)
                resp.begin()
                return resp

            monkeypatch.setattr(
                proxy_mod.urllib.request,
                "urlopen",
                create_autospec(orig_urlopen, side_effect=_fake_urlopen),
            )
            cli, srv = socket.socketpair()
            cli.settimeout(5.0)
            cli.sendall(raw_http_req)
            cli.shutdown(socket.SHUT_WR)
            outcome: dict[str, Any] = {}

            def _read_downstream() -> None:
                try:
                    resp = http.client.HTTPResponse(cli)
                    resp.begin()
                    outcome["status"] = resp.status
                    outcome["te"] = resp.getheader("Transfer-Encoding")
                    outcome["body"] = resp.read()
                except Exception as exc:
                    outcome["error"] = exc
                finally:
                    cli.close()

            reader = threading.Thread(target=_read_downstream, daemon=True)
            reader.start()
            try:
                proxy_mod.ProxyHandler(srv, ("127.0.0.1", 0), None)
            finally:
                try:
                    srv.close()
                except OSError:
                    pass
            reader.join(timeout=3.0)
            dl = time.monotonic() + 1.0
            while proxy_mod.health_payload()["in_flight"] != 0 and time.monotonic() < dl:
                time.sleep(0.01)
            assert proxy_mod.health_payload()["in_flight"] == 0
            return outcome

        # Case A: Content-Length declares 4096 bytes, upstream sends 24 bytes and disconnects mid-stream
        out_cl = _run_disconnect_scenario(
            [
                (
                    0.0,
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 4096\r\n\r\n"
                    b'{"jsonrpc":"2.0","part":',
                ),
                (0.05, b""),
            ]
        )
        assert (
            isinstance(out_cl.get("error"), http.client.IncompleteRead)
            or out_cl.get("status") == 502
        ), f"Truncated Content-Length returned as complete 200: {out_cl!r}"

        # Case B: Chunked response sends one chunk then disconnects without terminating 0\r\n\r\n
        out_chunked = _run_disconnect_scenario(
            [
                (
                    0.0,
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\n"
                    b"10\r\n0123456789abcdef\r\n",
                ),
                (0.05, b""),
            ]
        )
        assert (
            isinstance(out_chunked.get("error"), http.client.IncompleteRead)
            or out_chunked.get("status") == 502
        ), f"Truncated chunked stream returned as complete 200: {out_chunked!r}"

        # Case C: Upstream sends 200 OK with Content-Length: 1024 then drops immediately with 0 bytes
        out_empty_trunc = _run_disconnect_scenario(
            [
                (
                    0.0,
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 1024\r\n\r\n",
                ),
            ]
        )
        assert (
            isinstance(out_empty_trunc.get("error"), http.client.IncompleteRead)
            or out_empty_trunc.get("status") == 502
        ), f"Immediate disconnect after 200 headers returned as complete empty 200: {out_empty_trunc!r}"
    finally:
        proxy_mod.release_owner_lock()


@pytest.mark.enable_socket
def test_r6_item2_proxy_8943_dns_block_returns_504_on_time_with_zero_inflight_and_preserves_tls_sni(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6 Item 2: blocked DNS resolves in an isolated thread before in_flight is incremented, returns 504 on deadline with in_flight==0, and successful resolution connects via resolved IP while preserving TLS SNI."""
    import ssl
    import threading

    _clean_cap_env(monkeypatch)
    gp_lock_mod = _load_script_module(
        "_test_r6_gp_lock_item2", RESEARCH_RUNTIME_DIR / "guidepoint_lock.py"
    )
    proxy_mod = _load_script_module(
        "_test_r6_gp_proxy_item2", RESEARCH_RUNTIME_DIR / "scripts" / "guidepoint_mcp_proxy.py"
    )

    journal_file = tmp_path / "handoff-journal.json"
    token_file = tmp_path / "config" / "guidepoint_mcp_tokens.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(
        json.dumps(
            {
                "access_token": "tok-r6-dns",
                "refresh_token": "ref-r6-dns",
                "obtained_at": time.time(),
                "expires_in": 600,
            }
        ),
        encoding="utf-8",
    )
    gp_lock_mod.set_target_owner(gp_lock_mod.OWNER_LEGACY_PROXY, ownership_file=journal_file)
    monkeypatch.setenv("DALTON_GUIDEPOINT_OWNERSHIP_FILE", str(journal_file))
    monkeypatch.setenv("GUIDEPOINT_TOKENS_FILE", str(token_file))
    monkeypatch.setenv("GUIDEPOINT_CLIENT_ID", "cid-r6-dns")
    monkeypatch.setenv("GUIDEPOINT_CLIENT_SECRET", "csec-r6-dns")

    req_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_library", "arguments": {"query": "semis", "timeout": 0.4}},
        }
    ).encode("utf-8")
    raw_http_req = (
        b"POST /mcp HTTP/1.1\r\n"
        b"Host: 127.0.0.1:8943\r\n"
        b"X-Dalton-Request-Timeout-Ms: 400\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(req_body)}\r\n\r\n".encode("ascii")
        + req_body
    )

    proxy_mod.initialize_runtime(ownership_file=journal_file, token_store=token_file)
    orig_getaddrinfo = socket.getaddrinfo
    dns_release_a = threading.Event()
    dns_release_b = threading.Event()
    try:
        # Part A: Blocked UPSTREAM DNS -> 504 on time, in_flight == 0 both during & immediately after
        dns_entered_a = threading.Event()
        inflight_during_dns_a: list[int] = []

        def _blocked_upstream_dns(
            host: Any,
            port: Any,
            family: int = 0,
            type: int = 0,
            proto: int = 0,
            flags: int = 0,
        ) -> list[Any]:
            _ = (family, type, proto, flags)
            if host == "guidepoint-slow-dns.example":
                inflight_during_dns_a.append(proxy_mod.health_payload()["in_flight"])
                dns_entered_a.set()
                dns_release_a.wait(timeout=10.0)
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", int(port)))]
            return orig_getaddrinfo(host, port, family, type, proto, flags)

        monkeypatch.setattr(
            proxy_mod.socket,
            "getaddrinfo",
            create_autospec(orig_getaddrinfo, side_effect=_blocked_upstream_dns),
        )
        monkeypatch.setattr(proxy_mod, "UPSTREAM", "https://guidepoint-slow-dns.example/mcp")

        cli_a, srv_a = socket.socketpair()
        cli_a.settimeout(4.0)
        cli_a.sendall(raw_http_req)
        cli_a.shutdown(socket.SHUT_WR)
        rec_a = bytearray()

        def _drain_a() -> None:
            try:
                while True:
                    part = cli_a.recv(4096)
                    if not part:
                        break
                    rec_a.extend(part)
            finally:
                cli_a.close()

        ta = threading.Thread(target=_drain_a, daemon=True)
        ta.start()
        t0_a = time.monotonic()
        try:
            proxy_mod.ProxyHandler(srv_a, ("127.0.0.1", 0), None)
        finally:
            try:
                srv_a.close()
            except OSError:
                pass
        elapsed_a = time.monotonic() - t0_a
        ta.join(timeout=2.0)

        assert dns_entered_a.is_set(), "getaddrinfo was never invoked for UPSTREAM"
        assert elapsed_a < 1.8, f"Blocked upstream DNS exceeded 0.4s deadline: {elapsed_a:.2f}s"
        assert b"504" in bytes(rec_a)
        assert b"upstream_timeout" in bytes(rec_a)
        # Critical: DNS ran BEFORE in_flight increment, so in_flight is 0 even while DNS thread is still blocked!
        assert inflight_during_dns_a == [0]
        assert proxy_mod.health_payload()["in_flight"] == 0
        dns_release_a.set()

        # Part B: Blocked TOKEN_URL DNS when token is expired -> 504 on time, in_flight == 0 immediately
        proxy_mod._tokens["obtained_at"] = 1.0
        dns_entered_b = threading.Event()
        inflight_during_dns_b: list[int] = []

        def _blocked_token_dns(
            host: Any,
            port: Any,
            family: int = 0,
            type: int = 0,
            proto: int = 0,
            flags: int = 0,
        ) -> list[Any]:
            _ = (family, type, proto, flags)
            if host == "auth0-slow-dns.example":
                inflight_during_dns_b.append(proxy_mod.health_payload()["in_flight"])
                dns_entered_b.set()
                dns_release_b.wait(timeout=10.0)
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", int(port)))]
            return orig_getaddrinfo(host, port, family, type, proto, flags)

        monkeypatch.setattr(
            proxy_mod.socket,
            "getaddrinfo",
            create_autospec(orig_getaddrinfo, side_effect=_blocked_token_dns),
        )
        monkeypatch.setattr(proxy_mod, "TOKEN_URL", "https://auth0-slow-dns.example/oauth/token")

        cli_b, srv_b = socket.socketpair()
        cli_b.settimeout(4.0)
        cli_b.sendall(raw_http_req)
        cli_b.shutdown(socket.SHUT_WR)
        rec_b = bytearray()

        def _drain_b() -> None:
            try:
                while True:
                    part = cli_b.recv(4096)
                    if not part:
                        break
                    rec_b.extend(part)
            finally:
                cli_b.close()

        tb = threading.Thread(target=_drain_b, daemon=True)
        tb.start()
        t0_b = time.monotonic()
        try:
            proxy_mod.ProxyHandler(srv_b, ("127.0.0.1", 0), None)
        finally:
            try:
                srv_b.close()
            except OSError:
                pass
        elapsed_b = time.monotonic() - t0_b
        tb.join(timeout=2.0)

        assert dns_entered_b.is_set(), "getaddrinfo was never invoked for expired TOKEN_URL"
        assert elapsed_b < 1.8, f"Blocked token DNS exceeded 0.4s deadline: {elapsed_b:.2f}s"
        assert b"504" in bytes(rec_b)
        assert inflight_during_dns_b == [0]
        assert proxy_mod.health_payload()["in_flight"] == 0
        dns_release_b.set()

        # Part C: Successful DNS resolution connects directly to resolved IP (no second getaddrinfo)
        # while preserving TLS SNI (`server_hostname` == original hostname).
        proxy_mod._tokens["obtained_at"] = time.time()
        dns_calls_c: list[tuple[Any, Any]] = []
        connect_addrs_c: list[Any] = []
        sni_hostnames_c: list[str | None] = []
        orig_socket_cls = socket.socket

        def _resolved_dns_c(
            host: Any,
            port: Any,
            family: int = 0,
            type: int = 0,
            proto: int = 0,
            flags: int = 0,
        ) -> list[Any]:
            _ = (family, type, proto, flags)
            dns_calls_c.append((host, int(port)))
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", int(port)))]

        def _fake_sock_factory(
            family: int = socket.AF_INET,
            type: int = socket.SOCK_STREAM,
            proto: int = 0,
            fileno: Any = None,
        ) -> Any:
            if fileno is not None:
                return orig_socket_cls(family, type, proto, fileno)
            fake_sock = create_autospec(orig_socket_cls, instance=True)
            fake_sock.connect.side_effect = lambda addr: connect_addrs_c.append(addr)
            return fake_sock

        def _fake_wrap_socket(
            self_ctx: Any,
            sock: Any,
            server_side: bool = False,
            do_handshake_on_connect: bool = True,
            suppress_ragged_eofs: bool = True,
            server_hostname: Any = None,
            session: Any = None,
        ) -> Any:
            _ = (self_ctx, sock, server_side, do_handshake_on_connect, suppress_ragged_eofs, session)
            sni_hostnames_c.append(server_hostname)
            raise ssl.SSLError("stop after verifying SNI")

        cli_c, srv_c = socket.socketpair()
        cli_c.settimeout(4.0)
        cli_c.sendall(raw_http_req)
        cli_c.shutdown(socket.SHUT_WR)

        monkeypatch.setattr(
            proxy_mod.socket,
            "getaddrinfo",
            create_autospec(orig_getaddrinfo, side_effect=_resolved_dns_c),
        )
        monkeypatch.setattr(
            proxy_mod.socket,
            "socket",
            create_autospec(orig_socket_cls, side_effect=_fake_sock_factory),
        )
        monkeypatch.setattr(
            ssl.SSLContext,
            "wrap_socket",
            create_autospec(ssl.SSLContext.wrap_socket, side_effect=_fake_wrap_socket),
        )
        monkeypatch.setattr(proxy_mod, "UPSTREAM", "https://clapi.guidepoint.example:8443/mcp")

        rec_c = bytearray()

        def _drain_c() -> None:
            try:
                while True:
                    part = cli_c.recv(4096)
                    if not part:
                        break
                    rec_c.extend(part)
            finally:
                cli_c.close()

        tc = threading.Thread(target=_drain_c, daemon=True)
        tc.start()
        try:
            proxy_mod.ProxyHandler(srv_c, ("127.0.0.1", 0), None)
        finally:
            try:
                srv_c.close()
            except OSError:
                pass
        tc.join(timeout=2.0)

        assert dns_calls_c == [("clapi.guidepoint.example", 8443)]
        assert connect_addrs_c == [("127.0.0.1", 8443)]
        assert sni_hostnames_c == ["clapi.guidepoint.example"]
        assert b"502" in bytes(rec_c)
        assert proxy_mod.health_payload()["in_flight"] == 0
    finally:
        dns_release_a.set()
        dns_release_b.set()
        proxy_mod.release_owner_lock()


def test_r6_item3_host_direct_yaml_capabilities_with_and_without_pyyaml_and_requirements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6 Item 3: market-digest requirements declares PyYAML; both inline-flow `capabilities: {sales_notes: false}` and comment-bearing `capabilities: # ...` disable sales-notes with AND without PyYAML; unparseable fallback syntax fails closed instead of defaulting open."""
    _clean_cap_env(monkeypatch)

    # 1. Verify market-digest requirements.txt declares PyYAML
    req_file = RESEARCH_RUNTIME_DIR / "skills" / "market-digest" / "requirements.txt"
    req_lines = [
        line.strip().lower()
        for line in req_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert any(line.startswith("pyyaml") for line in req_lines), (
        f"PyYAML missing from {req_file}: {req_lines!r}"
    )

    # 2. Build an isolated mini-repo tree mirroring `src/dalton/` + `scripts/dalton/research_runtime/scripts/research_library.py`
    fake_repo = tmp_path / "repo"
    (fake_repo / "src" / "dalton").mkdir(parents=True)
    (fake_repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (fake_repo / "src" / "dalton" / "__init__.py").write_text("", encoding="utf-8")
    for fname in ("profile.py", "capabilities.py", "config_env.py"):
        shutil.copy2(REPO_ROOT / "src" / "dalton" / fname, fake_repo / "src" / "dalton" / fname)
    shutil.copy2(
        REPO_ROOT / "src" / "dalton" / "dalton.core.yaml",
        fake_repo / "src" / "dalton" / "dalton.core.yaml",
    )
    rl_dst = fake_repo / "scripts" / "dalton" / "research_runtime" / "scripts" / "research_library.py"
    rl_dst.parent.mkdir(parents=True)
    shutil.copy2(RESEARCH_RUNTIME_DIR / "scripts" / "research_library.py", rl_dst)
    shutil.copy2(
        RESEARCH_RUNTIME_DIR / "scripts" / "research_images.py",
        rl_dst.parent / "research_images.py",
    )

    # Create a PYTHONPATH shim that blocks `import yaml` to simulate a host venv without PyYAML
    no_yaml_shim = tmp_path / "no_yaml_shim"
    no_yaml_shim.mkdir()
    (no_yaml_shim / "sitecustomize.py").write_text(
        "import importlib.abc, sys\n"
        "class _BlockYaml(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path, target=None):\n"
        "        if fullname == 'yaml' or fullname.startswith('yaml.'):\n"
        "            raise ImportError('simulated missing PyYAML')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _BlockYaml())\n",
        encoding="utf-8",
    )

    def _probe_host_direct(yaml_text: str, *, with_pyyaml: bool) -> dict[str, Any]:
        (fake_repo / "src" / "dalton" / "dalton.yaml").write_text(yaml_text, encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "APP_ENV": "dalton",
            "DALTON_PROFILE": "everflow",
            "PYTHON_DOTENV_DISABLED": "1",
        }
        if not with_pyyaml:
            env["PYTHONPATH"] = str(no_yaml_shim)

        # Probe resolved sources directly AND invoke the real CLI on sales-notes
        code = (
            "import importlib.util, json, sys\n"
            "has_yaml = True\n"
            "try:\n"
            "    import yaml\n"
            "except ImportError:\n"
            "    has_yaml = False\n"
            f"spec = importlib.util.spec_from_file_location('rl', {str(rl_dst)!r})\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "print(json.dumps({'has_yaml': has_yaml, 'enabled': mod._enabled_library_sources(None)}))\n"
        )
        probe_res = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        probe_data = json.loads(probe_res.stdout)

        cli_res = subprocess.run(
            [sys.executable, str(rl_dst), "sales-notes", "--operation=list"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return {
            "has_yaml": probe_data["has_yaml"],
            "enabled": probe_data["enabled"],
            "cli_rc": cli_res.returncode,
            "cli_out": json.loads(cli_res.stdout),
        }

    # Form 1: inline flow mapping `capabilities: {sales_notes: false}`
    yaml_flow = "enabled: true\ncapabilities: {sales_notes: false}\n"
    for with_yaml in (True, False):
        res = _probe_host_direct(yaml_flow, with_pyyaml=with_yaml)
        assert res["has_yaml"] is with_yaml
        assert "sales-notes" not in res["enabled"], f"flow form leaked sales-notes (yaml={with_yaml}): {res}"
        assert "company-wiki" in res["enabled"], f"flow form unexpectedly dropped company-wiki (yaml={with_yaml}): {res}"
        assert res["cli_rc"] != 0
        assert res["cli_out"]["ok"] is False
        assert "capability_disabled" in str(res["cli_out"].get("error") or "")

    # Form 2: block mapping with trailing inline comments on `capabilities:` and entries
    yaml_commented = (
        "enabled: true # active\n"
        "capabilities: # deployment capability overrides\n"
        "  sales_notes: false # disable sales notes\n"
    )
    for with_yaml in (True, False):
        res = _probe_host_direct(yaml_commented, with_pyyaml=with_yaml)
        assert res["has_yaml"] is with_yaml
        assert "sales-notes" not in res["enabled"], f"commented form leaked sales-notes (yaml={with_yaml}): {res}"
        assert "company-wiki" in res["enabled"], f"commented form unexpectedly dropped company-wiki (yaml={with_yaml}): {res}"
        assert res["cli_rc"] != 0
        assert res["cli_out"]["ok"] is False
        assert "capability_disabled" in str(res["cli_out"].get("error") or "")

    # Form 3: complex multi-line flow mapping — PyYAML parses it (`sales-notes` off),
    # fallback without PyYAML cannot reliably parse it and MUST fail closed (`[]`, never default-open!)
    yaml_complex = "enabled: true\ncapabilities: {\n  sales_notes: false\n}\n"
    res_complex_yaml = _probe_host_direct(yaml_complex, with_pyyaml=True)
    assert res_complex_yaml["has_yaml"] is True
    assert "sales-notes" not in res_complex_yaml["enabled"]
    assert res_complex_yaml["cli_rc"] != 0

    res_complex_fallback = _probe_host_direct(yaml_complex, with_pyyaml=False)
    assert res_complex_fallback["has_yaml"] is False
    assert res_complex_fallback["enabled"] == [], (
        f"Unparseable capabilities syntax without PyYAML must fail closed ([]): {res_complex_fallback}"
    )
    assert res_complex_fallback["cli_rc"] != 0
    assert "capability_disabled" in str(res_complex_fallback["cli_out"].get("error") or "")
