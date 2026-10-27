"""Unit tests for unified Dalton system jobs management (`src/dalton/autonomy/system_jobs.py`)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import AsyncConnection

import scripts.dalton.researchdata_bridge as bridge_mod
import src.dalton.autonomy.preflight as preflight_mod
import src.dalton.autonomy.schedule as schedule_mod
import src.dalton.autonomy.system_jobs as system_jobs_mod
import src.dalton.autonomy.tracking as tracking_mod
import src.dalton.data_map.health as health_mod
import src.dalton.database.watch as watch_db
import src.server.database.automation as auto_db
import src.server.database.workspace as ws_db
from src.dalton.app import dalton_router
from src.dalton.tools.director_tools import build_director_tools
from src.server.utils.api import get_current_user_id


@pytest.fixture(autouse=True)
def _empty_coverage_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier lookups read the per-user coverage roster; these tests' fake
    connections are not a coverage store, so every ticker resolves as not on
    the roster (watch) unless a test patches its own tiers."""
    from src.dalton.coverage import get_user_tier as real_get_user_tier

    monkeypatch.setattr(
        tracking_mod,
        "get_user_tier",
        create_autospec(real_get_user_tier, return_value=None),
    )

USER_ID = "local-dev-user"
WS_NVDA = "11111111-1111-1111-1111-111111111111"
WS_HOME = "99999999-9999-9999-9999-999999999999"


def _make_tx_lock_conn() -> Any:
    """Build a spec'd ``AsyncConnection`` double with a serialized async ``transaction()`` context manager."""
    conn = create_autospec(AsyncConnection, instance=True)
    tx_lock = asyncio.Lock()

    @asynccontextmanager
    async def _tx_cm(*args: Any, **kwargs: Any):
        async with tx_lock:
            yield conn

    conn.transaction.side_effect = _tx_cm
    return conn


class _InMemoryAutomationsStore:
    """In-memory automations store wired via ``create_autospec`` to real ``auto_db`` signatures."""

    def __init__(self, initial_rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = [dict(r) for r in (initial_rows or [])]
        self._seq = len(self.rows) + 1
        self.lock_conn = _make_tx_lock_conn()
        self.observed_conns: dict[str, list[Any]] = {
            "lock": [],
            "list_all": [],
            "create": [],
            "update": [],
        }

    async def list_automations(
        self,
        user_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        filtered = [
            dict(r)
            for r in self.rows
            if r.get("user_id", user_id) == user_id
            and (status is None or r.get("status") == status)
        ]
        return filtered[offset : offset + limit], len(filtered)

    async def list_all_automations(
        self,
        user_id: str,
        *,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        self.observed_conns["list_all"].append(conn)
        await asyncio.sleep(0)
        return [
            dict(r)
            for r in self.rows
            if r.get("user_id", user_id) == user_id
        ]

    async def create_automation(
        self,
        user_id: str,
        name: str,
        trigger_type: str,
        instruction: str,
        *,
        file_name: str | None = None,
        description: str | None = None,
        cron_expression: str | None = None,
        timezone: str = "UTC",
        trigger_config: dict[str, Any] | None = None,
        next_run_at: Any = None,
        workspace_id: str | None = None,
        llm_model: str | None = None,
        additional_context: list[dict[str, Any]] | None = None,
        thread_strategy: str = "new",
        conversation_thread_id: str | None = None,
        max_failures: int = 3,
        delivery_config: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = "active",
        conn: Any = None,
    ) -> dict[str, Any]:
        self.observed_conns["create"].append(conn)
        await asyncio.sleep(0)
        auto_id = f"00000000-0000-0000-0000-{self._seq:012d}"
        self._seq += 1
        row = {
            "automation_id": auto_id,
            "user_id": user_id,
            "name": name,
            "file_name": file_name or f"{name}.json",
            "description": description,
            "trigger_type": trigger_type,
            "cron_expression": cron_expression,
            "timezone": timezone,
            "trigger_config": dict(trigger_config or {}),
            "next_run_at": next_run_at,
            "last_run_at": None,
            "instruction": instruction,
            "workspace_id": workspace_id,
            "llm_model": llm_model,
            "additional_context": additional_context,
            "thread_strategy": thread_strategy,
            "conversation_thread_id": conversation_thread_id,
            "status": status or "active",
            "max_failures": max_failures,
            "failure_count": 0,
            "disable_reason": None,
            "delivery_config": dict(delivery_config or {}),
            "metadata": dict(metadata or {}),
        }
        self.rows.append(row)
        return dict(row)

    async def update_automation(
        self,
        automation_id: str,
        user_id: str,
        *,
        conn: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        self.observed_conns["update"].append(conn)
        await asyncio.sleep(0)
        for r in self.rows:
            if str(r.get("automation_id")) == str(automation_id) and r.get("user_id", user_id) == user_id:
                for k, v in kwargs.items():
                    r[k] = v
                return dict(r)
        return None


def _wire_store(monkeypatch: pytest.MonkeyPatch, store: _InMemoryAutomationsStore) -> None:
    monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
    monkeypatch.setenv("DALTON_PM_USER_IDS", USER_ID)

    @asynccontextmanager
    async def _fake_get_db_conn():
        yield store.lock_conn

    async def _fake_lock_user(uid: str, *, conn: Any = None) -> None:
        store.observed_conns["lock"].append(conn)

    monkeypatch.setattr(
        schedule_mod,
        "get_db_connection",
        create_autospec(schedule_mod.get_db_connection, side_effect=_fake_get_db_conn),
    )
    monkeypatch.setattr(
        auto_db,
        "lock_user_automations",
        create_autospec(auto_db.lock_user_automations, side_effect=_fake_lock_user),
    )
    monkeypatch.setattr(
        auto_db,
        "list_automations",
        create_autospec(auto_db.list_automations, side_effect=store.list_automations),
    )
    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(auto_db.list_all_automations, side_effect=store.list_all_automations),
    )
    monkeypatch.setattr(
        auto_db,
        "create_automation",
        create_autospec(auto_db.create_automation, side_effect=store.create_automation),
    )
    monkeypatch.setattr(
        auto_db,
        "update_automation",
        create_autospec(auto_db.update_automation, side_effect=store.update_automation),
    )

    async def _fake_resolve_home(user_id: str) -> str:
        return WS_HOME

    monkeypatch.setattr(
        schedule_mod,
        "resolve_caller_home_workspace_id",
        create_autospec(schedule_mod.resolve_caller_home_workspace_id, side_effect=_fake_resolve_home),
    )

    async def _fake_get_workspace(workspace_id: str, *, conn: Any = None) -> dict[str, Any] | None:
        if str(workspace_id) == WS_NVDA:
            return {
                "workspace_id": WS_NVDA,
                "user_id": USER_ID,
                "name": "NVDA",
                "status": "running",
                "metadata": {"ticker": "NVDA"},
            }
        if str(workspace_id) == WS_HOME:
            return {
                "workspace_id": WS_HOME,
                "user_id": USER_ID,
                "name": "Home",
                "status": "running",
                "is_home": True,
                "metadata": {"is_home": True},
            }
        return None

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, side_effect=_fake_get_workspace),
    )
    monkeypatch.setattr(
        system_jobs_mod,
        "_discover_company_workspaces",
        create_autospec(
            system_jobs_mod._discover_company_workspaces,
            return_value=[WS_NVDA],
        ),
    )
    monkeypatch.setattr(
        tracking_mod,
        "resolve_workspace_id_by_ticker",
        create_autospec(
            tracking_mod.resolve_workspace_id_by_ticker,
            side_effect=lambda ticker, user_id: WS_NVDA if str(ticker).upper() == "NVDA" else None,
        ),
    )
    monkeypatch.setattr(
        system_jobs_mod,
        "resolve_workspace_id_by_ticker",
        create_autospec(
            system_jobs_mod.resolve_workspace_id_by_ticker,
            side_effect=lambda ticker, user_id: WS_NVDA if str(ticker).upper() == "NVDA" else None,
        ),
    )


@pytest.mark.asyncio
async def test_manifest_includes_four_scopes_and_derives_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1. Manifest contains all 4 scopes (company, home, host, ops) with accurate status derivation."""
    store = _InMemoryAutomationsStore()
    _wire_store(monkeypatch, store)

    # Enable NVDA tracking -> company status should be "on"
    await tracking_mod.enable_tracking(USER_ID, WS_NVDA)

    # Enable one Home job (`dalton-data-health`) -> "on"; others remain "off"
    await system_jobs_mod.toggle_system_job(USER_ID, "dalton-data-health", enabled=True)

    async def _fake_bridge(
        method: str, path: str, *, timeout_s: float = 8.0
    ) -> tuple[int, dict[str, Any] | None]:
        if path == "/jobs/conversation-archive":
            return 200, {
                "ok": True,
                "enabled": True,
                "status": "on",
                "last_run_at": "2026-10-22T13:15:00+00:00",
                "exit_code": 0,
                "last_result": "成功 (exit 0)",
                "log_tail": "[archive_daily] done",
                "next_run_at": "2026-10-23T21:15:00+08:00",
                "running": False,
            }
        if path == "/jobs/ops":
            return 200, {
                "ok": True,
                "jobs": [
                    {
                        "key": "ops-db-backup",
                        "kind": "ops_db_backup",
                        "scope": "ops",
                        "status": "on",
                        "last_run_at": "2026-10-23T03:00:00+08:00",
                        "last_result": "正常 (exit 0)",
                        "next_run_at": None,
                        "can_toggle": False,
                        "can_run_now": False,
                    }
                ],
            }
        return 404, None

    monkeypatch.setattr(
        system_jobs_mod,
        "_call_host_bridge",
        create_autospec(system_jobs_mod._call_host_bridge, side_effect=_fake_bridge),
    )

    manifest = await system_jobs_mod.list_system_jobs(USER_ID)
    assert manifest["ok"] is True
    jobs = manifest["jobs"]
    scopes_present = {j["scope"] for j in jobs}
    assert scopes_present == {"company", "home", "host", "ops"}

    required_fields = {
        "key",
        "kind",
        "scope",
        "status",
        "last_run_at",
        "last_result",
        "next_run_at",
        "can_toggle",
        "can_run_now",
    }
    for j in jobs:
        assert required_fields.issubset(j.keys()), f"Missing fields in {j}"
        assert j["status"] in ("on", "off", "partial", "unknown")

    nvda_job = next(j for j in jobs if j["key"] == "company:NVDA")
    assert nvda_job["status"] == "on"
    assert nvda_job["can_toggle"] is True
    assert nvda_job["can_run_now"] is True

    # Pause one of NVDA's 4 tasks -> company status becomes "partial"
    nvda_watch = next(r for r in store.rows if r["name"] == "dalton-nvda-watch")
    await store.update_automation(nvda_watch["automation_id"], USER_ID, status="paused")
    manifest_partial = await system_jobs_mod.list_system_jobs(USER_ID)
    nvda_partial = next(j for j in manifest_partial["jobs"] if j["key"] == "company:NVDA")
    assert nvda_partial["status"] == "partial"

    health_job = next(j for j in jobs if j["key"] == "dalton-data-health")
    assert health_job["scope"] == "home"
    assert health_job["status"] == "on"

    cal_job = next(j for j in jobs if j["key"] == "dalton-earnings-calendar")
    assert cal_job["scope"] == "home"
    assert cal_job["status"] == "off"

    host_job = next(j for j in jobs if j["key"] == "conversation-archive")
    assert host_job["scope"] == "host"
    assert host_job["status"] == "on"
    assert host_job["config"]["prompt_version"] == system_jobs_mod.get_dalton_config().conversations.prompt_version
    assert host_job["config"]["extractor_model"]

    ops_backup = next(j for j in jobs if j["key"] == "ops-db-backup")
    assert ops_backup["scope"] == "ops"
    assert ops_backup["status"] == "on"
    assert ops_backup["can_toggle"] is False
    assert ops_backup["can_run_now"] is False


@pytest.mark.asyncio
async def test_home_global_jobs_enable_pause_resume_and_paused_by_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2. Home global jobs support enable, pause (writing paused_by_user), and resume."""
    store = _InMemoryAutomationsStore()
    _wire_store(monkeypatch, store)

    # Enable dalton-director-daily
    res_on = await system_jobs_mod.toggle_system_job(
        USER_ID, "dalton-director-daily", enabled=True
    )
    assert res_on["job"]["status"] == "on"
    assert res_on["job"]["next_run_at"] is not None

    # Pause dalton-director-daily -> status == "off", metadata.paused_by_user is True
    res_off = await system_jobs_mod.toggle_system_job(
        USER_ID, "dalton-director-daily", enabled=False
    )
    assert res_off["job"]["status"] == "off"
    assert res_off["job"]["paused_by_user"] is True
    stored_row = next(r for r in store.rows if r["name"] == "dalton-director-daily")
    assert stored_row["status"] == "paused"
    assert stored_row["metadata"].get("paused_by_user") is True

    # Non-forced auto-enable respects paused_by_user
    await system_jobs_mod.toggle_system_job(
        USER_ID,
        "dalton-director-daily",
        enabled=True,
        force=False,
        respect_user_pause=True,
    )
    still_paused = next(r for r in store.rows if r["name"] == "dalton-director-daily")
    assert still_paused["status"] == "paused"

    # Explicit user resume clears paused_by_user and restores active status
    res_resumed = await system_jobs_mod.toggle_system_job(
        USER_ID,
        "dalton-director-daily",
        enabled=True,
        force=True,
    )
    assert res_resumed["job"]["status"] == "on"
    assert res_resumed["job"]["paused_by_user"] is False
    resumed_row = next(r for r in store.rows if r["name"] == "dalton-director-daily")
    assert resumed_row["status"] == "active"
    assert "paused_by_user" not in resumed_row["metadata"]


@pytest.mark.asyncio
async def test_bridge_conversation_archive_auth_lock_sanitization_and_shell_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3. Bridge `/jobs/conversation-archive*` enforces token auth, returns already_running on lock conflict, sanitizes logs, and archive_daily.sh exits 0 when disabled."""
    state_file = tmp_path / "conversation-archive-state.json"
    status_file = tmp_path / "conversation-archive-status.json"
    log_file = tmp_path / "conversation-archive.log"
    env_file = tmp_path / "bridge.env"
    env_file.write_text(
        "DALTON_BRIDGE_TOKEN=bridge-secret-token\nDALTON_BRIDGE_ADMIN_TOKEN=bridge-admin-secret-token\n",
        encoding="utf-8",
    )
    log_file.write_text(
        "Running archive with Bearer secret-token-1234567890 and /Users/alice/secret/path\n"
        "Cookie: a=1; b=2; sid=super-secret-cookie\n"
        "Set-Cookie: session_id=xyz987654321; Path=/private/tmp/work\n"
        "leaked_hex=0123456789abcdef0123456789abcdef01234567\n"
        "leaked_b64=YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY3ODkw==\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(env_file))
    monkeypatch.delenv("DALTON_BRIDGE_TOKEN", raising=False)
    monkeypatch.delenv("DALTON_BRIDGE_ADMIN_TOKEN", raising=False)
    cfg = bridge_mod.BridgeConfig()
    cfg.token = "bridge-secret-token"
    cfg.admin_token = "bridge-admin-secret-token"
    cfg.archive_state_file = state_file
    cfg.archive_status_file = status_file
    cfg.archive_log_file = log_file
    cfg.repo_root = Path(__file__).resolve().parents[3]
    bridge = bridge_mod.ResearchDataBridge(cfg)

    async def _send_raw_http(method: str, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        reader = asyncio.StreamReader()
        hdr_lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1"]
        for hk, hv in (headers or {}).items():
            hdr_lines.append(f"{hk}: {hv}")
        raw_req = ("\r\n".join(hdr_lines) + "\r\n\r\n").encode("utf-8")
        reader.feed_data(raw_req)
        reader.feed_eof()
        buf = bytearray()
        writer = create_autospec(asyncio.StreamWriter, instance=True)
        writer.write.side_effect = buf.extend
        writer.drain.return_value = None
        writer.close.return_value = None
        writer.wait_closed.return_value = None
        await bridge_mod._handle_client(bridge, reader, writer)
        resp_text = buf.decode("utf-8", errors="replace")
        head, body_str = resp_text.split("\r\n\r\n", 1)
        status_code = int(head.splitlines()[0].split(" ")[1])
        return status_code, (json.loads(body_str) if body_str.strip() else {})

    # Unauthorized request -> 401
    code_unauth, _ = await _send_raw_http("GET", "/jobs/conversation-archive")
    assert code_unauth == 401

    # MCP token is rejected on admin endpoints -> 403
    code_mcp_tok, _ = await _send_raw_http(
        "GET",
        "/jobs/conversation-archive",
        {"Authorization": "Bearer bridge-secret-token"},
    )
    assert code_mcp_tok in (401, 403)

    auth_headers = {"Authorization": "Bearer bridge-admin-secret-token"}

    # Authorized GET -> returns sanitized log_tail (multi-cookie, Bearer, paths, 40+ char hex/b64 all redacted)
    code_get, data = await _send_raw_http("GET", "/jobs/conversation-archive", auth_headers)
    assert code_get == 200
    assert data["status"] == "on"
    assert "secret-token-1234567890" not in data["log_tail"]
    assert "super-secret-cookie" not in data["log_tail"]
    assert "b=2" not in data["log_tail"]
    assert "xyz987654321" not in data["log_tail"]
    assert "0123456789abcdef0123456789abcdef01234567" not in data["log_tail"]
    assert "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY3ODkw" not in data["log_tail"]
    assert "/Users/alice" not in data["log_tail"]
    assert "/private/tmp" not in data["log_tail"]
    assert "[REDACTED]" in data["log_tail"]
    assert "<local>" in data["log_tail"]

    # Disable via POST /jobs/conversation-archive/disable
    code_dis, dis_body = await _send_raw_http("POST", "/jobs/conversation-archive/disable", auth_headers)
    assert code_dis == 200
    assert dis_body["enabled"] is False
    assert json.loads(state_file.read_text(encoding="utf-8"))["enabled"] is False

    # Run archive_daily.sh when disabled -> exits 0 and records "已关闭"
    script_path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "dalton"
        / "launchd"
        / "archive_daily.sh"
    )
    env = {
        **os.environ,
        "DALTON_ARCHIVE_STATE_FILE": str(state_file),
        "DALTON_ARCHIVE_STATUS_FILE": str(status_file),
    }
    proc_res = await asyncio.to_thread(
        subprocess.run,
        ["/bin/bash", str(script_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc_res.returncode == 0
    assert "已关闭" in proc_res.stdout
    status_saved = json.loads(status_file.read_text(encoding="utf-8"))
    assert status_saved["exit_code"] == 0
    assert status_saved["last_result"] == "已关闭"

    # GET /jobs/conversation-archive preserves saved last_result ("已关闭") instead of overwriting with "成功 (exit_code=0)"
    code_get_after_dis, data_after_dis = await _send_raw_http("GET", "/jobs/conversation-archive", auth_headers)
    assert code_get_after_dis == 200
    assert data_after_dis["last_result"] == "已关闭"

    # Simulate in-flight process lock conflict via create_autospec -> returns already_running
    running_proc = create_autospec(asyncio.subprocess.Process, instance=True)
    running_proc.pid = 42424
    running_proc.returncode = None

    bridge._archive_proc = running_proc
    code_conflict, conflict_body = await _send_raw_http(
        "POST", "/jobs/conversation-archive/run", auth_headers
    )
    assert code_conflict == 200
    assert conflict_body["status"] == "already_running"
    assert conflict_body["already_running"] is True


@pytest.mark.asyncio
async def test_data_health_preflight_runs_health_checks_and_skips_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4. `dalton-data-health` runs `run_all_health_checks` during preflight and finishes with skip (should_wake=False)."""
    mock_health = create_autospec(
        health_mod.run_all_health_checks,
        return_value={
            "ok": True,
            "checked_count": 12,
            "available_count": 11,
            "unavailable_count": 1,
            "results": [],
        },
    )
    monkeypatch.setattr(health_mod, "run_all_health_checks", mock_health)
    monkeypatch.setattr(
        watch_db,
        "insert_preflight_log",
        create_autospec(watch_db.insert_preflight_log, return_value=None),
    )
    monkeypatch.setattr(
        preflight_mod,
        "verify_workspace_access",
        create_autospec(
            preflight_mod.verify_workspace_access,
            return_value={"workspace_id": WS_HOME, "user_id": USER_ID},
        ),
    )

    automation = {
        "automation_id": "00000000-0000-0000-0000-000000000100",
        "user_id": USER_ID,
        "workspace_id": WS_HOME,
        "name": "dalton-data-health",
        "trigger_type": "cron",
        "cron_expression": "0 7 * * *",
        "metadata": {"dalton_managed": True, "dalton_kind": "data_health"},
    }
    res = await preflight_mod.preflight_check(automation, "exec-data-health-001")
    assert res.should_wake is False
    assert res.decision == "skip"
    assert res.skip is True
    assert "11/12" in (res.reason or "")
    mock_health.assert_called_once_with(persist=True)


@pytest.mark.asyncio
async def test_ops_jobs_fallback_to_unknown_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5. When host bridge fails or is unreachable, ops jobs return status='unknown' without raising."""
    store = _InMemoryAutomationsStore()
    _wire_store(monkeypatch, store)

    async def _failing_bridge(
        method: str, path: str, *, timeout_s: float = 8.0
    ) -> tuple[int, dict[str, Any] | None]:
        raise RuntimeError("bridge connection refused")

    monkeypatch.setattr(
        system_jobs_mod,
        "_call_host_bridge",
        create_autospec(system_jobs_mod._call_host_bridge, side_effect=_failing_bridge),
    )

    manifest = await system_jobs_mod.list_system_jobs(USER_ID)
    assert manifest["ok"] is True
    ops_jobs = [j for j in manifest["jobs"] if j["scope"] == "ops"]
    assert [j["key"] for j in ops_jobs] == ["ops-db-backup", "ops-backend-watchdog"]
    for item in ops_jobs:
        assert item["status"] == "unknown"
        assert item["can_toggle"] is False
        assert item["can_run_now"] is False


@pytest.mark.asyncio
async def test_permission_boundaries_for_analyst_ops_and_non_pm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6. Permission boundaries: company analyst cannot touch Home/host/other company jobs; nobody can toggle ops jobs; non-PM cannot mutate Home/host via HTTP."""
    monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
    monkeypatch.setenv("DALTON_PM_USER_IDS", USER_ID)
    store = _InMemoryAutomationsStore()
    _wire_store(monkeypatch, store)

    analyst_tools = {t.name: t for t in build_director_tools(role="analyst")}
    cos_tools = {t.name: t for t in build_director_tools(role="chief_of_staff")}

    analyst_sys_tool = analyst_tools["dalton_system_jobs"]
    cos_sys_tool = cos_tools["dalton_system_jobs"]

    analyst_cfg = {"configurable": {"user_id": USER_ID, "workspace_id": WS_NVDA}}
    cos_cfg = {"configurable": {"user_id": USER_ID, "workspace_id": "home"}}

    # Analyst refused on Home job
    out_home = json.loads(
        await analyst_sys_tool.ainvoke(
            {"action": "enable", "key": "dalton-data-health"},
            config=analyst_cfg,
        )
    )
    assert out_home["ok"] is False
    assert out_home["error"] == "forbidden_scope"

    # Analyst refused on Host job
    out_host = json.loads(
        await analyst_sys_tool.ainvoke(
            {"action": "run_now", "key": "conversation-archive"},
            config=analyst_cfg,
        )
    )
    assert out_host["ok"] is False
    assert out_host["error"] == "forbidden_scope"

    # Analyst refused on another company's job
    out_other_co = json.loads(
        await analyst_sys_tool.ainvoke(
            {"action": "enable", "key": "company:GOOGL"},
            config=analyst_cfg,
        )
    )
    assert out_other_co["ok"] is False
    assert out_other_co["error"] == "forbidden_workspace"

    # Analyst can enable their own company's tracking via dalton_system_jobs
    out_own_co = json.loads(
        await analyst_sys_tool.ainvoke(
            {"action": "enable", "key": "company:NVDA"},
            config=analyst_cfg,
        )
    )
    assert out_own_co["ok"] is True
    assert out_own_co["status"] == "enabled"

    # Chief of Staff refused when trying to toggle or run ops job
    out_cos_ops = json.loads(
        await cos_sys_tool.ainvoke(
            {"action": "enable", "key": "ops-db-backup"},
            config=cos_cfg,
        )
    )
    assert out_cos_ops["ok"] is False
    assert out_cos_ops["error"] == "ops_readonly"

    # HTTP Router: non-PM user gets 403 on Home/host mutations, and PM gets 403 on ops mutations
    app = FastAPI()
    current_uid = {"uid": "non-pm-member"}
    app.dependency_overrides[get_current_user_id] = lambda: current_uid["uid"]
    app.include_router(dalton_router)
    http_client = TestClient(app, raise_server_exceptions=False)

    r_home_non_pm = http_client.post(
        "/api/v1/dalton/system-jobs/dalton-data-health",
        json={"enabled": True},
    )
    assert r_home_non_pm.status_code == 403

    r_host_non_pm = http_client.post(
        "/api/v1/dalton/system-jobs/conversation-archive/run",
    )
    assert r_host_non_pm.status_code == 403

    current_uid["uid"] = USER_ID  # PM user
    r_ops_pm = http_client.post(
        "/api/v1/dalton/system-jobs/ops-db-backup",
        json={"enabled": False},
    )
    assert r_ops_pm.status_code == 403

    # Verify Chief of Staff `run_now` states asynchronous execution and where to check results
    monkeypatch.setattr(
        system_jobs_mod,
        "_trigger_automation_row_async",
        create_autospec(
            system_jobs_mod._trigger_automation_row_async,
            return_value={
                "execution_id": "exec-now-123",
                "automation_id": "00000000-0000-0000-0000-000000000001",
                "status": "triggered",
            },
        ),
    )
    out_run_now = json.loads(
        await cos_sys_tool.ainvoke(
            {"action": "run_now", "key": "dalton-data-health"},
            config=cos_cfg,
        )
    )
    assert out_run_now["ok"] is True
    assert out_run_now["async"] is True
    assert "异步" in out_run_now["message"]
    assert out_run_now["where_to_check"]
    assert out_run_now["where_to_check"] in out_run_now["message"]

    # Non-PM Chief of Staff rejected when calling `enable` on host archive via tool (Item 1)
    non_pm_cos_cfg = {"configurable": {"user_id": "non-pm-member", "workspace_id": "home"}}
    out_non_pm_tool = json.loads(
        await cos_sys_tool.ainvoke(
            {"action": "enable", "key": "conversation-archive"},
            config=non_pm_cos_cfg,
        )
    )
    assert out_non_pm_tool["ok"] is False
    assert out_non_pm_tool["error"] == "forbidden_pm"

    # Non-PM rejected at service layer for both Home and Host toggle/run_now
    with pytest.raises(PermissionError):
        await system_jobs_mod.toggle_system_job("non-pm-member", "conversation-archive", enabled=True)
    with pytest.raises(PermissionError):
        await system_jobs_mod.run_system_job_now("non-pm-member", "dalton-data-health")


@pytest.mark.asyncio
async def test_home_job_paused_not_implicitly_resumed_by_bootstrap_and_run_now_no_pause_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 2: User disables `dalton-data-health` -> running `bootstrap_watch --include-global` upsert path keeps it paused; `run_now` on uncreated Home job creates paused row without `paused_by_user`."""
    store = _InMemoryAutomationsStore()
    _wire_store(monkeypatch, store)

    # 1) User disables `dalton-data-health` directly (even before row existed -> created directly as paused)
    res_off = await system_jobs_mod.toggle_system_job(USER_ID, "dalton-data-health", enabled=False)
    assert res_off["job"]["status"] == "off"
    row = next(r for r in store.rows if r["name"] == "dalton-data-health")
    assert row["status"] == "paused"
    assert row["metadata"].get("paused_by_user") is True

    # 2) Run the `bootstrap_watch.py --include-global` upsert path (`build_global_director_specs` + `schedule_dalton_automation_helper`)
    for spec in schedule_mod.build_global_director_specs():
        await schedule_mod.schedule_dalton_automation_helper(
            spec["name"],
            spec,
            user_id=USER_ID,
            workspace_id=WS_HOME,
            role="chief_of_staff",
        )

    after_bootstrap = next(r for r in store.rows if r["name"] == "dalton-data-health")
    assert after_bootstrap["status"] == "paused"
    assert after_bootstrap["metadata"].get("paused_by_user") is True

    # 3) `run_now` on a Home job that has no row yet creates a paused row WITHOUT `paused_by_user`
    store.rows.clear()
    monkeypatch.setattr(
        system_jobs_mod,
        "_trigger_automation_row_async",
        create_autospec(
            system_jobs_mod._trigger_automation_row_async,
            return_value={
                "execution_id": "exec-weekly-001",
                "automation_id": "00000000-0000-0000-0000-000000000099",
                "status": "triggered",
            },
        ),
    )
    run_res = await system_jobs_mod.run_system_job_now(USER_ID, "dalton-director-weekly")
    assert run_res["ok"] is True
    weekly_row = next(r for r in store.rows if r["name"] == "dalton-director-weekly")
    assert weekly_row["status"] == "paused"
    assert "paused_by_user" not in weekly_row["metadata"]


@pytest.mark.asyncio
async def test_home_jobs_over_200_rows_and_concurrent_toggle_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 3: >200 automations do not hide Home jobs; concurrent Home toggles under `user_automations_lock` create only 1 row."""
    filler_rows = [
        {
            "automation_id": f"filler-{i:04d}",
            "user_id": USER_ID,
            "name": f"custom-job-{i:04d}",
            "trigger_type": "cron",
            "instruction": "noop",
            "status": "active",
            "metadata": {},
        }
        for i in range(250)
    ]
    store = _InMemoryAutomationsStore(initial_rows=filler_rows)
    _wire_store(monkeypatch, store)

    # Concurrent enable of the same Home job creates only 1 `dalton-director-daily` row
    r1, r2 = await asyncio.gather(
        system_jobs_mod.toggle_system_job(USER_ID, "dalton-director-daily", enabled=True),
        system_jobs_mod.toggle_system_job(USER_ID, "dalton-director-daily", enabled=True),
    )
    assert r1["job"]["status"] == "on"
    assert r2["job"]["status"] == "on"
    daily_rows = [r for r in store.rows if r["name"] == "dalton-director-daily"]
    assert len(daily_rows) == 1

    # Pause with >250 rows in store -> status and paused_by_user are still accurate
    await system_jobs_mod.toggle_system_job(USER_ID, "dalton-director-daily", enabled=False)
    st = await system_jobs_mod.get_system_job_status(USER_ID, "dalton-director-daily")
    assert st["job"]["status"] == "off"
    assert st["job"]["paused_by_user"] is True


@pytest.mark.asyncio
async def test_data_health_ignores_autonomy_disabled_and_timeout_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items 5 & 6: `data_health` runs even when `autonomy.enabled=False`, never wakes agent, uses `data_health_timeout_s`, and records timeout/failure in preflight log and `last_result`."""
    import src.dalton.settings as settings_mod

    cfg = settings_mod.get_dalton_settings().model_copy(
        update={
            "autonomy": settings_mod.get_dalton_settings().autonomy.model_copy(
                update={"enabled": False, "data_health_timeout_s": 0.05}
            )
        }
    )
    monkeypatch.setattr(
        preflight_mod,
        "get_dalton_settings",
        create_autospec(preflight_mod.get_dalton_settings, return_value=cfg),
    )
    monkeypatch.setattr(
        preflight_mod,
        "verify_workspace_access",
        create_autospec(
            preflight_mod.verify_workspace_access,
            return_value={"workspace_id": WS_HOME, "user_id": USER_ID},
        ),
    )

    logged_entries: list[dict[str, Any]] = []

    async def _capture_log(**kwargs: Any) -> None:
        logged_entries.append(dict(kwargs))

    monkeypatch.setattr(
        watch_db,
        "insert_preflight_log",
        create_autospec(watch_db.insert_preflight_log, side_effect=_capture_log),
    )

    # 1) Even with autonomy.enabled=False, data_health runs and never wakes agent
    async def _slow_health(*, persist: bool = True, **kw: Any) -> dict[str, Any]:
        await asyncio.sleep(0.3)
        return {"checked_count": 10, "available_count": 10}

    monkeypatch.setattr(
        health_mod,
        "run_all_health_checks",
        create_autospec(health_mod.run_all_health_checks, side_effect=_slow_health),
    )

    automation = {
        "automation_id": "00000000-0000-0000-0000-000000000100",
        "user_id": USER_ID,
        "workspace_id": WS_HOME,
        "name": "dalton-data-health",
        "trigger_type": "cron",
        "cron_expression": "0 7 * * *",
        "metadata": {"dalton_managed": True, "dalton_kind": "data_health"},
    }
    res_timeout = await preflight_mod.preflight_check(automation, "exec-dh-timeout")
    assert res_timeout.should_wake is False
    assert res_timeout.skip is True
    assert res_timeout.reason == "体检超时"
    assert any(e.get("reason") == "体检超时" for e in logged_entries)

    # Verify _summarize_last_execution maps timeout/failure/completion reasons accurately
    assert (
        system_jobs_mod._summarize_last_execution(
            {"status": "skipped", "error": "体检超时"},
            kind="data_health",
        )[1]
        == "体检超时"
    )
    assert (
        system_jobs_mod._summarize_last_execution(
            {"status": "skipped", "error": "体检失败：RuntimeError"},
            kind="data_health",
        )[1]
        == "体检失败：RuntimeError"
    )
    assert (
        system_jobs_mod._summarize_last_execution(
            {"status": "skipped", "error": "data_health completed (11/12 available)"},
            kind="data_health",
        )[1]
        == "体检完成 (11/12 available)"
    )


@pytest.mark.asyncio
async def test_bridge_delayed_exit_code_2_and_timeout_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 7: Delayed exit code 2 (>0.5s) records `已在运行，跳过`; subprocess timeout terminates, kills, closes log, clears `_archive_proc`, and removes PID file."""
    cfg = bridge_mod.BridgeConfig()
    cfg.archive_state_file = tmp_path / "state.json"
    cfg.archive_status_file = tmp_path / "status.json"
    cfg.archive_log_file = tmp_path / "archive.log"
    cfg.archive_run_timeout_s = 0.05
    monkeypatch.setenv("DALTON_ARCHIVE_PID_FILE", str(tmp_path / "conversation-archive.pid"))
    bridge = bridge_mod.ResearchDataBridge(cfg)

    # 1) Delayed exit code 2 (exits after startup probe in _watch_archive_proc)
    proc_conflict = create_autospec(asyncio.subprocess.Process, instance=True)
    proc_conflict.pid = 54321
    proc_conflict.returncode = 2

    async def _wait_exit_2() -> int:
        await asyncio.sleep(0.01)
        return 2

    proc_conflict.wait.side_effect = _wait_exit_2
    bridge._write_archive_pid_file(54321)
    bridge._archive_proc = proc_conflict
    await bridge._watch_archive_proc(proc_conflict, "2026-10-23T00:00:00+00:00")
    saved_2 = json.loads(cfg.archive_status_file.read_text(encoding="utf-8"))
    assert saved_2["exit_code"] == 2
    assert saved_2["last_result"] == "已在运行，跳过"
    assert bridge._archive_proc is None
    assert not (tmp_path / "conversation-archive.pid").exists()

    # 2) Subprocess timeout -> terminate -> kill -> wait -> "运行超时"
    proc_hung = create_autospec(asyncio.subprocess.Process, instance=True)
    proc_hung.pid = 54322
    proc_hung.returncode = None

    wait_calls = {"n": 0}

    async def _hung_wait() -> int:
        wait_calls["n"] += 1
        if wait_calls["n"] == 1:
            await asyncio.sleep(1.0)
        proc_hung.returncode = -9
        return -9

    proc_hung.wait.side_effect = _hung_wait
    bridge._write_archive_pid_file(54322)
    bridge._archive_proc = proc_hung
    log_fh = cfg.archive_log_file.open("ab")
    await bridge._watch_archive_proc(proc_hung, "2026-10-23T01:00:00+00:00", log_fh)
    assert log_fh.closed
    proc_hung.terminate.assert_called_once()
    assert bridge._archive_proc is None
    assert not (tmp_path / "conversation-archive.pid").exists()
    saved_timeout = json.loads(cfg.archive_status_file.read_text(encoding="utf-8"))
    assert saved_timeout["last_result"] == "运行超时"


@pytest.mark.asyncio
async def test_non_pm_strips_log_tail_and_company_run_now_requires_enabled_and_aliases_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items 8, 10, 11, 12, 15: Non-PM responses strip `log_tail`; company `run_now` without watch row refuses; batch aliases rejected; bridge URL parsed via `urllib.parse`; `_trigger_automation_row_async` only falls back on not-found."""
    import src.dalton.settings as settings_mod

    store = _InMemoryAutomationsStore()
    _wire_store(monkeypatch, store)

    async def _fake_bridge(
        method: str, path: str, *, timeout_s: float = 8.0
    ) -> tuple[int, dict[str, Any] | None]:
        if path == "/jobs/conversation-archive":
            return 200, {
                "ok": True,
                "enabled": True,
                "status": "on",
                "last_run_at": "2026-10-22T13:15:00+00:00",
                "exit_code": 0,
                "last_result": "成功 (exit_code=0)",
                "log_tail": "some sanitized log tail",
                "next_run_at": "2026-10-23T21:15:00+08:00",
                "running": False,
            }
        return 200, {"ok": True, "jobs": []}

    monkeypatch.setattr(
        system_jobs_mod,
        "_call_host_bridge",
        create_autospec(system_jobs_mod._call_host_bridge, side_effect=_fake_bridge),
    )

    # Item 8: Non-PM gets log_tail=None, PM gets log_tail
    pm_list = await system_jobs_mod.list_system_jobs(USER_ID)
    pm_host = next(j for j in pm_list["jobs"] if j["key"] == "conversation-archive")
    assert pm_host["log_tail"] == "some sanitized log tail"

    non_pm_list = await system_jobs_mod.list_system_jobs("non-pm-user")
    non_pm_host = next(j for j in non_pm_list["jobs"] if j["key"] == "conversation-archive")
    assert non_pm_host.get("log_tail") is None
    assert "log_tail" not in non_pm_host

    # Item 10: Company `run_now` when `dalton-nvda-watch` is missing raises ValueError and does not enable tracking
    mock_enable = create_autospec(tracking_mod.enable_tracking)
    monkeypatch.setattr(tracking_mod, "enable_tracking", mock_enable)
    with pytest.raises(ValueError, match="未开启自动跟踪"):
        await system_jobs_mod.run_system_job_now(USER_ID, "company:NVDA")
    assert mock_enable.await_count == 0

    # Item 11: Batch aliases `home`, `home:all`, `global` are rejected
    for bad_key in ("home", "home:all", "global"):
        with pytest.raises(ValueError):
            system_jobs_mod.classify_system_job_key(bad_key)

    # Item 12 (and R3 Item 11): Bridge URL parsing via urllib.parse, clearing both DALTON_BRIDGE_BASE_URL and DALTON_BRIDGE_URL
    monkeypatch.delenv("DALTON_BRIDGE_BASE_URL", raising=False)
    monkeypatch.delenv("DALTON_BRIDGE_URL", raising=False)
    monkeypatch.delenv("DALTON_BRIDGE_TOKEN", raising=False)
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", "/nonexistent-env-file")
    monkeypatch.setenv("DALTON_RESEARCHDATA_URL", "http://host.docker.internal:8960/mcp")
    assert settings_mod.get_dalton_bridge_base_url() == "http://host.docker.internal:8960"
    monkeypatch.setenv("DALTON_RESEARCHDATA_URL", "http://127.0.0.1:8960/mcp/legacy-token-xyz")
    assert settings_mod.get_dalton_bridge_base_url() == "http://127.0.0.1:8960"
    assert settings_mod.get_dalton_bridge_token() == "legacy-token-xyz"

    # Item 15: `_trigger_automation_row_async` does NOT fall back when trigger_automation fails with a post-creation RuntimeError
    async def _boom_after_insert(automation_id: str, user_id: str) -> dict[str, Any]:
        raise RuntimeError("enqueue failed after execution row insert")

    monkeypatch.setattr(
        system_jobs_mod.auto_lifecycle,
        "trigger_automation",
        create_autospec(system_jobs_mod.auto_lifecycle.trigger_automation, side_effect=_boom_after_insert),
    )
    mock_create_exec = create_autospec(system_jobs_mod.exec_db.create_execution)
    monkeypatch.setattr(system_jobs_mod.exec_db, "create_execution", mock_create_exec)

    with pytest.raises(RuntimeError, match="enqueue failed"):
        await system_jobs_mod._trigger_automation_row_async(
            USER_ID,
            {"automation_id": "auto-1"},
        )
    assert mock_create_exec.await_count == 0


def test_install_conversation_archive_refuses_overwrite_without_force(
    tmp_path: Path,
) -> None:
    """Item 14: `install_conversation_archive.sh` refuses to overwrite existing plist and prints `diff -u` unless `--force` is given."""
    script_path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "dalton"
        / "install_conversation_archive.sh"
    )
    fake_home = tmp_path / "home"
    plist_dest = fake_home / "Library" / "LaunchAgents" / "com.dalton.conversation-archive.plist"
    plist_dest.parent.mkdir(parents=True, exist_ok=True)
    plist_dest.write_text("<plist>old-custom-content</plist>\n", encoding="utf-8")

    env = {
        **os.environ,
        "HOME": str(fake_home),
        "DALTON_ARCHIVE_PLIST_DEST": str(plist_dest),
        "DALTON_SKIP_LAUNCHCTL": "1",
    }

    # Without --force: exits non-zero, prints unified diff, and leaves existing file untouched
    res_refuse = subprocess.run(
        ["/bin/bash", str(script_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res_refuse.returncode != 0
    combined_out = res_refuse.stdout + res_refuse.stderr
    assert "---" in combined_out and "+++" in combined_out
    assert plist_dest.read_text(encoding="utf-8") == "<plist>old-custom-content</plist>\n"

    # With --force: overwrites plist and exits 0
    res_force = subprocess.run(
        ["/bin/bash", str(script_path), "--force"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res_force.returncode == 0
    assert "com.dalton.conversation-archive" in plist_dest.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_preflight_400s_heartbeat_keeps_execution_alive_beyond_300s_stale_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 Item 3: Heartbeat task starts before `dalton_preflight.decide` so a 400s preflight (simulated clock) never exceeds the 300s stale threshold."""
    import src.server.services.automation_executor as exec_mod

    sim_clock = {"now": 1000.0, "last_heartbeat": 1000.0}
    heartbeat_timestamps: list[float] = []
    max_observed_Heartbeat_gap = {"gap": 0.0}
    tick_ready = asyncio.Event()
    tick_done = asyncio.Event()

    async def _fake_touch_execution(execution_id: str) -> None:
        sim_clock["last_heartbeat"] = sim_clock["now"]
        heartbeat_timestamps.append(sim_clock["now"])

    monkeypatch.setattr(
        exec_mod.exec_db,
        "touch_execution",
        create_autospec(exec_mod.exec_db.touch_execution, side_effect=_fake_touch_execution),
    )

    real_sleep = asyncio.sleep

    async def _simulated_sleep(delay: float) -> None:
        if delay == exec_mod._HEARTBEAT_SECONDS:
            await tick_ready.wait()
            tick_ready.clear()
            await real_sleep(0)
            tick_done.set()
            return
        await real_sleep(0)

    monkeypatch.setattr(exec_mod.asyncio, "sleep", _simulated_sleep)

    async def _400s_preflight_decide(
        automation: dict[str, Any],
        execution_id: str,
    ) -> preflight_mod.PreflightResult:
        # Advance simulated clock from t=1000 to t=1400 (400s total > 300s ABANDONED_AFTER_SECONDS) in 60s heartbeat steps
        for _ in range(6):
            sim_clock["now"] += float(exec_mod._HEARTBEAT_SECONDS)
            gap_before_touch = sim_clock["now"] - sim_clock["last_heartbeat"]
            assert gap_before_touch < exec_mod.ABANDONED_AFTER_SECONDS
            tick_ready.set()
            await tick_done.wait()
            tick_done.clear()
            gap_after_touch = sim_clock["now"] - sim_clock["last_heartbeat"]
            max_observed_Heartbeat_gap["gap"] = max(max_observed_Heartbeat_gap["gap"], gap_before_touch)
            assert gap_after_touch == 0.0
        sim_clock["now"] += 40.0  # Total elapsed = 360 + 40 = 400.0s
        assert (sim_clock["now"] - sim_clock["last_heartbeat"]) < exec_mod.ABANDONED_AFTER_SECONDS
        return preflight_mod.PreflightResult(
            should_wake=False,
            decision="skip",
            reason="体检完成 (12/12 available)",
            signals=[],
        )

    monkeypatch.setattr(
        preflight_mod,
        "decide",
        create_autospec(preflight_mod.decide, side_effect=_400s_preflight_decide),
    )
    mock_settle = create_autospec(exec_mod.settle, return_value=True)
    monkeypatch.setattr(exec_mod, "settle", mock_settle)

    executor = exec_mod.AutomationExecutor()
    await executor.execute(
        {
            "automation_id": "00000000-0000-0000-0000-000000000777",
            "user_id": USER_ID,
            "workspace_id": WS_HOME,
            "name": "dalton-data-health",
            "trigger_type": "cron",
        },
        "exec-400s-health",
    )

    assert sim_clock["now"] - 1000.0 == 400.0
    assert len(heartbeat_timestamps) == 6
    assert max_observed_Heartbeat_gap["gap"] < exec_mod.ABANDONED_AFTER_SECONDS
    mock_settle.assert_awaited_once()


@pytest.mark.asyncio
async def test_bridge_state_write_oserror_returns_500_and_ok_false_and_cookie_over_8kib_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 Items 5, 6, 7, 8, 9, 10:
    - Item 5: State file write OSError returns HTTP 500 with Chinese reason and `ok=False` in both bridge and `system_jobs.toggle_system_job`.
    - Item 6: `Cookie:` line > 8 KiB is read in full, sanitized first, and truncated to 8 KiB without leaking its tail.
    - Item 7: `_summarize_last_execution` returns raw `reason` / `excerpt` when `data_health` reason matches neither timeout, failure, nor completion.
    - Item 8: `archive_daily.sh` logs error and exits non-zero when `STATE_FILE` is corrupted or unreadable.
    - Item 9: `handle_run_conversation_archive` cleans up on pre-watcher error; `_watch_archive_proc` re-raises `CancelledError` and keeps `_archive_proc`/PID/`log_fh` if process is still alive after final `wait()`.
    - Item 10: Bridge and `archive_daily.sh` share status-dir `flock` for atomic PID check-and-write; losing side never overwrites or deletes live PID.
    """
    store = _InMemoryAutomationsStore()
    _wire_store(monkeypatch, store)

    cfg = bridge_mod.BridgeConfig()
    cfg.archive_state_file = tmp_path / "state.json"
    cfg.archive_status_file = tmp_path / "status.json"
    cfg.archive_log_file = tmp_path / "archive.log"
    cfg.archive_err_log_file = tmp_path / "archive.err.log"
    pid_file = tmp_path / "conversation-archive.pid"
    monkeypatch.setenv("DALTON_ARCHIVE_PID_FILE", str(pid_file))
    bridge = bridge_mod.ResearchDataBridge(cfg)

    # --- Item 5 & R4 Item 1: Writing archive state file raises OSError -> bridge returns 500 + ok=False, toggle_system_job raises RuntimeError, router returns HTTP 500 with Chinese reason ---
    def _boom_replace(src: Any, dst: Any) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as mp:
        mp.setattr(bridge_mod.os, "replace", create_autospec(bridge_mod.os.replace, side_effect=_boom_replace))
        code_500, body_500 = await bridge.handle_toggle_conversation_archive(False)
        assert code_500 == 500
        assert body_500["ok"] is False
        assert "失败" in body_500["error"]

        async def _bridge_500(
            method: str, path: str, *, timeout_s: float = 8.0
        ) -> tuple[int, dict[str, Any] | None]:
            return code_500, body_500

        mp.setattr(
            system_jobs_mod,
            "_call_host_bridge",
            create_autospec(system_jobs_mod._call_host_bridge, side_effect=_bridge_500),
        )
        with pytest.raises(RuntimeError, match="切换团队对话归档状态失败"):
            await system_jobs_mod.toggle_system_job(
                USER_ID,
                "conversation-archive",
                enabled=False,
            )

        app = FastAPI()
        app.dependency_overrides[get_current_user_id] = lambda: USER_ID
        app.include_router(dalton_router)
        http_client = TestClient(app, raise_server_exceptions=False)
        resp_toggle_500 = http_client.post(
            "/api/v1/dalton/system-jobs/conversation-archive",
            json={"enabled": False},
        )
        assert resp_toggle_500.status_code == 500
        assert "切换团队对话归档状态失败" in resp_toggle_500.json().get("detail", "")

    # --- Item 6: Cookie line > 8 KiB is read as a complete line, sanitized before 8 KiB truncation, and never leaks tail ---
    long_cookie_line = "Cookie: session=head_secret;" + (" padding_key=padding_val;" * 450) + " tail_secret=LEAKED_COOKIE_TAIL_999"
    assert len(long_cookie_line.encode("utf-8")) > 10000
    cfg.archive_log_file.write_text(
        "2026-10-23T21:15:00Z [INFO] starting archive\n" + long_cookie_line + "\n",
        encoding="utf-8",
    )
    sanitized_tail = bridge._read_sanitized_archive_log_tail()
    assert "LEAKED_COOKIE_TAIL_999" not in sanitized_tail
    assert "head_secret" not in sanitized_tail
    assert "Cookie: [REDACTED]" in sanitized_tail
    assert len(sanitized_tail.encode("utf-8")) <= 8192

    # --- Item 7: `_summarize_last_execution` returns raw reason/excerpt instead of hardcoded "体检完成" on custom reason ---
    _, custom_summary = system_jobs_mod._summarize_last_execution(
        {"status": "skipped", "skip_reason": "precheck", "excerpt": "数据源配置未变更，跳过本轮探测"},
        kind="data_health",
    )
    assert custom_summary == "数据源配置未变更，跳过本轮探测"
    assert custom_summary != "体检完成"

    # --- Item 8: `archive_daily.sh` logs error and exits non-zero when state file is corrupted ---
    script_path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "dalton"
        / "launchd"
        / "archive_daily.sh"
    )
    cfg.archive_state_file.write_text("{corrupted-json-not-valid", encoding="utf-8")
    marker_file = tmp_path / "should_not_run.marker"
    fake_archive_py = tmp_path / "fake_archive.py"
    fake_archive_py.write_text(
        f"from pathlib import Path\nPath({str(marker_file)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    corrupt_res = await asyncio.to_thread(
        subprocess.run,
        ["/bin/bash", str(script_path)],
        env={
            **os.environ,
            "DALTON_ARCHIVE_STATE_FILE": str(cfg.archive_state_file),
            "DALTON_ARCHIVE_STATUS_FILE": str(cfg.archive_status_file),
            "DALTON_ARCHIVE_PID_FILE": str(pid_file),
            "DALTON_CONVERSATION_ARCHIVE_PY": str(fake_archive_py),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert corrupt_res.returncode != 0
    assert "ERROR" in (corrupt_res.stderr + corrupt_res.stdout)
    assert not marker_file.exists()

    # Restore valid state file for Item 10
    cfg.archive_state_file.write_text('{"enabled": true}\n', encoding="utf-8")

    # --- Item 10 & R4 Item 2: Atomic PID check-and-write under shared status-dir flock; distinguishes already_running vs error ---
    live_winner_pid = os.getpid()
    assert bridge._write_archive_pid_file(live_winner_pid) == "written"
    assert pid_file.read_text(encoding="utf-8").strip() == str(live_winner_pid)
    assert (cfg.archive_status_file.with_name(f"{cfg.archive_status_file.name}.lock")).exists()

    # Losing bridge side attempting to write a different PID sees live winner PID -> returns "already_running"
    assert bridge._write_archive_pid_file(live_winner_pid + 99999) == "already_running"
    assert pid_file.read_text(encoding="utf-8").strip() == str(live_winner_pid)

    # Losing bridge side attempting to remove with a different expected_pid does not delete live winner PID
    bridge._remove_archive_pid_file(expected_pid=live_winner_pid + 99999)
    assert pid_file.read_text(encoding="utf-8").strip() == str(live_winner_pid)

    # Losing `archive_daily.sh` side sees live winner PID under flock, exits without overwriting or deleting winner PID
    busy_res = await asyncio.to_thread(
        subprocess.run,
        ["/bin/bash", str(script_path)],
        env={
            **os.environ,
            "DALTON_ARCHIVE_STATE_FILE": str(cfg.archive_state_file),
            "DALTON_ARCHIVE_STATUS_FILE": str(cfg.archive_status_file),
            "DALTON_ARCHIVE_PID_FILE": str(pid_file),
            "DALTON_CONVERSATION_ARCHIVE_PY": str(fake_archive_py),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert busy_res.returncode == 0
    assert "已在运行" in busy_res.stdout
    assert pid_file.exists()
    assert pid_file.read_text(encoding="utf-8").strip() == str(live_winner_pid)
    bridge._remove_archive_pid_file(expected_pid=live_winner_pid)
    assert not pid_file.exists()

    # --- R4 Item 2: PID write failure returns "error" (not "already_running"), bridge returns 500 + Chinese error and reaps spawned proc; run_system_job_now rejects 200 + ok:false ---
    proc_pid_err = create_autospec(asyncio.subprocess.Process, instance=True)
    proc_pid_err.pid = 76543
    proc_pid_err.returncode = None

    async def _reap_wait() -> int:
        proc_pid_err.returncode = -15
        return -15

    proc_pid_err.wait.side_effect = _reap_wait
    with monkeypatch.context() as mp:
        mp.setattr(
            bridge_mod.asyncio,
            "create_subprocess_exec",
            create_autospec(bridge_mod.asyncio.create_subprocess_exec, return_value=proc_pid_err),
        )
        mp.setattr(bridge_mod.os, "replace", create_autospec(bridge_mod.os.replace, side_effect=_boom_replace))
        assert bridge._write_archive_pid_file(76543) == "error"
        code_pid_500, body_pid_500 = await bridge.handle_run_conversation_archive()
        assert code_pid_500 == 500
        assert body_pid_500["ok"] is False
        assert body_pid_500["status"] == "error"
        assert "写入团队对话归档 PID 文件失败" in body_pid_500["error"]
        proc_pid_err.terminate.assert_called_once()
        assert bridge._archive_proc is None

        # run_system_job_now must NOT treat 200 with ok:false as success
        async def _bridge_200_ok_false(
            method: str, path: str, *, timeout_s: float = 8.0
        ) -> tuple[int, dict[str, Any] | None]:
            return 200, {
                "ok": False,
                "key": "conversation-archive",
                "status": "already_running",
                "already_running": True,
                "error": "已有归档在运行",
            }

        mp.setattr(
            system_jobs_mod,
            "_call_host_bridge",
            create_autospec(system_jobs_mod._call_host_bridge, side_effect=_bridge_200_ok_false),
        )
        with pytest.raises(RuntimeError, match="已有归档在运行"):
            await system_jobs_mod.run_system_job_now(USER_ID, "conversation-archive")

    # --- Item 9a: `handle_run_conversation_archive` cleans up `log_fh`, terminates `proc`, and removes PID file if an error occurs before watcher registration ---
    cfg.archive_startup_probe_s = 0.01
    proc_spawned = create_autospec(asyncio.subprocess.Process, instance=True)
    proc_spawned.pid = 65432
    proc_spawned.returncode = None
    spawn_wait_calls = {"n": 0}

    async def _wait_after_term() -> int:
        spawn_wait_calls["n"] += 1
        if spawn_wait_calls["n"] == 1:
            await asyncio.sleep(1.0)
        proc_spawned.returncode = -15
        return -15

    proc_spawned.wait.side_effect = _wait_after_term

    def _boom_create_task(coro: Any, *args: Any, **kwargs: Any) -> Any:
        coro.close()
        raise RuntimeError("event loop create_task failed")

    with monkeypatch.context() as mp:
        mp.setattr(
            bridge_mod.asyncio,
            "create_subprocess_exec",
            create_autospec(bridge_mod.asyncio.create_subprocess_exec, return_value=proc_spawned),
        )
        mp.setattr(
            bridge_mod.asyncio,
            "create_task",
            create_autospec(bridge_mod.asyncio.create_task, side_effect=_boom_create_task),
        )
        with pytest.raises(RuntimeError, match="event loop create_task failed"):
            await bridge.handle_run_conversation_archive()

    proc_spawned.terminate.assert_called_once()
    assert bridge._archive_proc is None
    assert not pid_file.exists()

    # --- R4 Item 3: Startup stage final `wait` failure keeps `_archive_proc`, PID file, and open `log_fh`, and re-raises `CancelledError` ---
    proc_startup_immortal = create_autospec(asyncio.subprocess.Process, instance=True)
    proc_startup_immortal.pid = live_winner_pid
    proc_startup_immortal.returncode = None
    startup_wait_step = {"n": 0}

    async def _startup_immortal_wait() -> int:
        startup_wait_step["n"] += 1
        if startup_wait_step["n"] == 1:
            await asyncio.sleep(1.0)
        if startup_wait_step["n"] == 2:
            raise asyncio.CancelledError()
        raise RuntimeError("final wait after kill failed")

    proc_startup_immortal.wait.side_effect = _startup_immortal_wait
    opened_log_handles: list[Any] = []
    real_log_open = Path.open

    def _track_log_open(self_path: Path, *args: Any, **kwargs: Any) -> Any:
        fh = real_log_open(self_path, *args, **kwargs)
        if self_path == cfg.archive_log_file:
            opened_log_handles.append(fh)
        return fh

    with monkeypatch.context() as mp:
        mp.setattr(Path, "open", _track_log_open)
        mp.setattr(
            bridge_mod.asyncio,
            "create_subprocess_exec",
            create_autospec(bridge_mod.asyncio.create_subprocess_exec, return_value=proc_startup_immortal),
        )
        mp.setattr(
            bridge_mod.asyncio,
            "create_task",
            create_autospec(bridge_mod.asyncio.create_task, side_effect=_boom_create_task),
        )
        try:
            with pytest.raises(asyncio.CancelledError):
                await bridge.handle_run_conversation_archive()
            proc_startup_immortal.terminate.assert_called_once()
            proc_startup_immortal.kill.assert_called_once()
            assert bridge._archive_proc is proc_startup_immortal
            assert pid_file.exists()
            assert opened_log_handles and not opened_log_handles[-1].closed
        finally:
            for fh in opened_log_handles:
                if not fh.closed:
                    fh.close()
            bridge._archive_proc = None
            bridge._remove_archive_pid_file(expected_pid=live_winner_pid)

    # --- Item 9b: `_watch_archive_proc` re-raises `CancelledError` and keeps `_archive_proc`, PID file, and `log_fh` when process is still alive after final `wait()` ---
    proc_immortal = create_autospec(asyncio.subprocess.Process, instance=True)
    proc_immortal.pid = live_winner_pid
    proc_immortal.returncode = None

    wait_step = {"n": 0}

    async def _immortal_wait() -> int:
        wait_step["n"] += 1
        if wait_step["n"] == 1:
            raise asyncio.CancelledError()
        raise RuntimeError("wait failed, process still alive")

    proc_immortal.wait.side_effect = _immortal_wait
    bridge._write_archive_pid_file(live_winner_pid)
    bridge._archive_proc = proc_immortal
    immortal_log_fh = cfg.archive_log_file.open("ab")
    try:
        with pytest.raises(asyncio.CancelledError):
            await bridge._watch_archive_proc(
                proc_immortal,
                "2026-10-23T02:00:00+00:00",
                immortal_log_fh,
            )
        # Process still has returncode=None -> _archive_proc, PID file, and log_fh must NOT be cleared/closed
        assert bridge._archive_proc is proc_immortal
        assert pid_file.exists()
        assert not immortal_log_fh.closed
    finally:
        immortal_log_fh.close()
        bridge._archive_proc = None
        bridge._remove_archive_pid_file(expected_pid=live_winner_pid)

    # --- R4 Item 1 & Item 6: Frontend DaltonSystemJobsCard checks `res?.ok === true` and WorkspaceTrackingToggle imports hooks from the Dalton-local `./useWorkspaceTracking` ---
    repo_root = Path(__file__).resolve().parents[3]
    card_src = (
        repo_root / "web/src/pages/Settings/panels/dalton/DaltonSystemJobsCard.tsx"
    ).read_text(encoding="utf-8")
    assert "res?.ok === true" in card_src

    toggle_src = (
        repo_root / "web/src/pages/ChatAgent/components/dalton/WorkspaceTrackingToggle.tsx"
    ).read_text(encoding="utf-8")
    assert "from './useWorkspaceTracking'" in toggle_src
    assert "export function useWorkspaceTracking" not in toggle_src
    assert "export function useUpdateWorkspaceTracking" not in toggle_src


@pytest.mark.asyncio
async def test_bridge_admin_token_separation_and_backend_callers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify bridge separates MCP token (`DALTON_BRIDGE_TOKEN`) from admin token (`DALTON_BRIDGE_ADMIN_TOKEN`),
    fails closed when admin token is unset, and backend callers (`embedder.py`, `system_jobs.py`) use `get_dalton_bridge_admin_token()`.
    """
    import httpx

    import src.dalton.conversations.embedder as embedder_mod
    import src.dalton.settings as settings_mod

    state_file = tmp_path / "archive-state.json"
    status_file = tmp_path / "archive-status.json"
    log_file = tmp_path / "archive.log"
    env_file = tmp_path / "researchdata-bridge.env"
    env_file.write_text(
        "DALTON_BRIDGE_TOKEN=mcp-sandbox-token-111\n"
        "DALTON_BRIDGE_ADMIN_TOKEN=admin-host-token-222\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(env_file))
    monkeypatch.delenv("DALTON_BRIDGE_TOKEN", raising=False)
    monkeypatch.delenv("DALTON_BRIDGE_ADMIN_TOKEN", raising=False)

    cfg = bridge_mod.BridgeConfig()
    assert cfg.token == "mcp-sandbox-token-111"
    assert cfg.admin_token == "admin-host-token-222"
    cfg.archive_state_file = state_file
    cfg.archive_status_file = status_file
    cfg.archive_log_file = log_file
    cfg.repo_root = Path(__file__).resolve().parents[3]
    bridge = bridge_mod.ResearchDataBridge(cfg)

    monkeypatch.setattr(
        bridge,
        "handle_embed",
        create_autospec(
            bridge.handle_embed,
            return_value=(200, {"ok": True, "embeddings": [[0.1, 0.2]], "model": "gemini-embedding-001"}),
        ),
    )
    monkeypatch.setattr(
        bridge,
        "handle_get_ops_jobs",
        create_autospec(
            bridge.handle_get_ops_jobs,
            return_value=(200, {"ok": True, "jobs": [{"key": "ops-db-backup"}]}),
        ),
    )

    async def _send_http_req(
        target_bridge: bridge_mod.ResearchDataBridge,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body_obj: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        reader = asyncio.StreamReader()
        body_bytes = json.dumps(body_obj).encode("utf-8") if body_obj is not None else b""
        hdr_lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1:8960"]
        if body_bytes:
            hdr_lines.append("Content-Type: application/json")
            hdr_lines.append(f"Content-Length: {len(body_bytes)}")
        for hk, hv in (headers or {}).items():
            hdr_lines.append(f"{hk}: {hv}")
        raw_req = ("\r\n".join(hdr_lines) + "\r\n\r\n").encode("utf-8") + body_bytes
        reader.feed_data(raw_req)
        reader.feed_eof()
        buf = bytearray()
        writer = create_autospec(asyncio.StreamWriter, instance=True)
        writer.write.side_effect = buf.extend
        writer.drain.return_value = None
        writer.close.return_value = None
        writer.wait_closed.return_value = None
        await bridge_mod._handle_client(target_bridge, reader, writer)
        resp_text = buf.decode("utf-8", errors="replace")
        head, body_str = resp_text.split("\r\n\r\n", 1)
        status_code = int(head.splitlines()[0].split(" ")[1])
        return status_code, (json.loads(body_str) if body_str.strip() else {})

    mcp_headers = {"Authorization": "Bearer mcp-sandbox-token-111"}
    admin_headers = {"Authorization": "Bearer admin-host-token-222"}

    # 1. Calling /embed, /jobs/conversation-archive/disable, /jobs/ops with MCP token returns 401/403
    code_embed_mcp, _ = await _send_http_req(
        bridge, "POST", "/embed", headers=mcp_headers, body_obj={"texts": ["hello"]}
    )
    assert code_embed_mcp in (401, 403)

    code_disable_mcp, _ = await _send_http_req(
        bridge, "POST", "/jobs/conversation-archive/disable", headers=mcp_headers
    )
    assert code_disable_mcp in (401, 403)

    code_ops_mcp, _ = await _send_http_req(
        bridge, "GET", "/jobs/ops", headers=mcp_headers
    )
    assert code_ops_mcp in (401, 403)

    # Future non-MCP routes (e.g. /notify) also go through _require_admin_token before routing
    code_future_mcp, _ = await _send_http_req(
        bridge, "POST", "/notify", headers=mcp_headers, body_obj={"msg": "hi"}
    )
    assert code_future_mcp in (401, 403)

    # 2. Calling /embed, /jobs/conversation-archive/disable, /jobs/ops with admin token succeeds (200)
    code_embed_admin, body_embed_admin = await _send_http_req(
        bridge, "POST", "/embed", headers=admin_headers, body_obj={"texts": ["hello"]}
    )
    assert code_embed_admin == 200
    assert body_embed_admin["ok"] is True

    code_disable_admin, body_disable_admin = await _send_http_req(
        bridge, "POST", "/jobs/conversation-archive/disable", headers=admin_headers
    )
    assert code_disable_admin == 200
    assert body_disable_admin["ok"] is True
    assert body_disable_admin["enabled"] is False

    code_ops_admin, body_ops_admin = await _send_http_req(
        bridge, "GET", "/jobs/ops", headers=admin_headers
    )
    assert code_ops_admin == 200
    assert body_ops_admin["ok"] is True

    # 3. MCP endpoint (/mcp) accepts both MCP token and admin token
    ping_rpc = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    code_mcp_tok, body_mcp_tok = await _send_http_req(
        bridge, "POST", "/mcp", headers=mcp_headers, body_obj=ping_rpc
    )
    assert code_mcp_tok == 200
    assert body_mcp_tok == {"jsonrpc": "2.0", "id": 1, "result": {}}

    code_mcp_admin, body_mcp_admin = await _send_http_req(
        bridge, "POST", "/mcp", headers=admin_headers, body_obj=ping_rpc
    )
    assert code_mcp_admin == 200
    assert body_mcp_admin == {"jsonrpc": "2.0", "id": 1, "result": {}}

    # 4. When DALTON_BRIDGE_ADMIN_TOKEN is NOT configured, all admin endpoints fail closed (401/403) and startup logs warning
    env_no_admin = tmp_path / "no-admin.env"
    env_no_admin.write_text("DALTON_BRIDGE_TOKEN=mcp-sandbox-token-111\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(env_no_admin))
    monkeypatch.delenv("DALTON_BRIDGE_TOKEN", raising=False)
    monkeypatch.delenv("DALTON_BRIDGE_ADMIN_TOKEN", raising=False)

    caplog.clear()
    with caplog.at_level("WARNING"):
        cfg_no_admin = bridge_mod.BridgeConfig()
        assert cfg_no_admin.admin_token == ""
        bridge_no_admin = bridge_mod.ResearchDataBridge(cfg_no_admin)
    assert any("DALTON_BRIDGE_ADMIN_TOKEN" in rec.message for rec in caplog.records)

    for method, path, body in (
        ("POST", "/embed", {"texts": ["hello"]}),
        ("POST", "/jobs/conversation-archive/disable", None),
        ("GET", "/jobs/ops", None),
    ):
        code_fail_closed, _ = await _send_http_req(
            bridge_no_admin, method, path, headers=mcp_headers, body_obj=body
        )
        assert code_fail_closed in (401, 403)

    # MCP still works when admin token is unset
    code_mcp_still_ok, _ = await _send_http_req(
        bridge_no_admin, "POST", "/mcp", headers=mcp_headers, body_obj=ping_rpc
    )
    assert code_mcp_still_ok == 200

    # 5. Backend callers (`embedder.py` and `system_jobs.py`) use `get_dalton_bridge_admin_token()`, while `data_map/health.py` uses `get_dalton_bridge_token()`
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", "mcp-token-val")
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "admin-token-val")
    assert settings_mod.get_dalton_bridge_token() == "mcp-token-val"
    assert settings_mod.get_dalton_bridge_admin_token() == "admin-token-val"

    mock_post_embed = create_autospec(
        embedder_mod._post_embed_sync,
        return_value={"ok": True, "embeddings": [[0.5, 0.25]], "model": "gemini-embedding-001"},
    )
    monkeypatch.setattr(embedder_mod, "_post_embed_sync", mock_post_embed)
    vecs, _ = await embedder_mod.embed_texts_via_bridge(["doc text"])
    assert vecs == [[0.5, 0.25]]
    assert mock_post_embed.call_args.args[1] == "admin-token-val"

    captured_headers: dict[str, str] = {}
    mock_resp = create_autospec(httpx.Response, instance=True)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True}

    async def _fake_request(method: str, url: str, headers: dict[str, str] | None = None) -> Any:
        captured_headers.update(headers or {})
        return mock_resp

    mock_client = create_autospec(httpx.AsyncClient, instance=True)
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.request.side_effect = _fake_request
    monkeypatch.setattr(
        system_jobs_mod.httpx,
        "AsyncClient",
        create_autospec(system_jobs_mod.httpx.AsyncClient, return_value=mock_client),
    )

    st_code, payload = await system_jobs_mod._call_host_bridge("GET", "/jobs/ops")
    assert st_code == 200
    assert payload == {"ok": True}
    assert captured_headers.get("Authorization") == "Bearer admin-token-val"
    assert captured_headers.get("X-Dalton-Bridge-Token") == "admin-token-val"

    _, health_tok, _ = health_mod.resolve_researchdata_endpoint_and_token()
    assert health_tok == "mcp-token-val"


def test_plugins_config_and_sandbox_env_exclude_bridge_admin_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify sandbox output boundaries (`PTCSandbox._build_sandbox_env_vars` and
    `ToolFunctionGenerator.generate_client_config`) scrub `DALTON_BRIDGE_ADMIN_TOKEN`
    by actual value after `src/ptc_agent/config/plugins.py` expands `${VAR}` placeholders:
    1) referencing admin token via another env variable name;
    2) carrying admin token in URL (and headers);
    3) carrying admin token in startup args.
    """
    import ptc_agent.config.plugins as plugins_mod
    from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
    from ptc_agent.core.tool_generator import ToolFunctionGenerator

    admin_secret = "super-secret-bridge-admin-token-do-not-leak"
    mcp_secret = "sandbox-mcp-token-allowed"
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", admin_secret)
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", mcp_secret)
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "internal-platform-secret")

    # Build a synthetic built-in plugins directory loaded through the real plugins.py loader
    test_plugins_dir = tmp_path / "plugins"
    bundle_dir = test_plugins_dir / "test_admin_token_guard"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "test_admin_token_guard",
                "version": "1.0.0",
                "description": "Test bundle for bridge admin token boundary scrubbing",
            }
        ),
        encoding="utf-8",
    )
    (bundle_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "leak_env_srv": {
                        "type": "stdio",
                        "command": "python3",
                        "args": ["env_server.py"],
                        "env": {
                            "CUSTOM_ADMIN_ENV_KEY": "${DALTON_BRIDGE_ADMIN_TOKEN}",
                            "SAFE_VAR": "safe-value",
                        },
                    },
                    "leak_url_srv": {
                        "type": "streamable-http",
                        "url": "http://host.docker.internal:8960/mcp/${DALTON_BRIDGE_ADMIN_TOKEN}",
                        "headers": {
                            "X-Custom-Admin": "Bearer ${DALTON_BRIDGE_ADMIN_TOKEN}",
                        },
                    },
                    "leak_args_srv": {
                        "type": "stdio",
                        "command": "python3",
                        "args": [
                            "args_server.py",
                            "--admin-token=${DALTON_BRIDGE_ADMIN_TOKEN}",
                            "--normal-flag=1",
                        ],
                    },
                    "clean_mcp_srv": {
                        "type": "streamable-http",
                        "url": "http://host.docker.internal:8960/mcp/${DALTON_BRIDGE_TOKEN}",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(plugins_mod, "BUNDLES_DIR", test_plugins_dir)

    # Load through the real plugins.py loader (which expands ${DALTON_BRIDGE_ADMIN_TOKEN} into plaintext)
    loaded_servers = plugins_mod.bundled_mcp_servers()
    by_name = {s.name: s for s in loaded_servers}
    assert by_name["leak_env_srv"].env["CUSTOM_ADMIN_ENV_KEY"] == admin_secret
    assert admin_secret in (by_name["leak_url_srv"].url or "")
    assert any(admin_secret in str(a) for a in by_name["leak_args_srv"].args)

    class _TrustedProvider:
        # B3's builder gates the deployment env on provider trust; the stub
        # host is trusted so the token paths under test run.
        @staticmethod
        def trusted_host(config):
            return True

    sandbox_stub = create_autospec(PTCSandbox, instance=True)
    sandbox_stub.provider = _TrustedProvider()
    # The real builder delegates to _deployment_env_vars off the instance;
    # on the autospec stub that dispatch would hit a mock, so delegate back.
    sandbox_stub._deployment_env_vars.side_effect = (
        lambda bindings: PTCSandbox._deployment_env_vars(sandbox_stub, bindings)
    )
    # Upstream's env-var build now consults self.config for platform-secret
    # withholding; a Docker provider never withholds, which keeps this test on
    # the plain Dalton (oss) path.
    sandbox_stub.config = SimpleNamespace(sandbox=SimpleNamespace(provider="docker"))
    sandbox_stub._token_file_path = "/home/workspace/.ginlix_token"
    sandbox_stub._builtin_servers.return_value = loaded_servers

    caplog.clear()
    with caplog.at_level("WARNING"):
        env_vars = PTCSandbox._build_sandbox_env_vars(sandbox_stub, {})
        gen = ToolFunctionGenerator()
        mcp_client_cfg = gen.generate_client_config(loaded_servers)

    # 1. Final sandbox env_vars and generated MCP client config never contain the admin token value
    serialized_env = json.dumps(env_vars)
    serialized_cfg = json.dumps(mcp_client_cfg)
    assert admin_secret not in serialized_env
    assert admin_secret not in serialized_cfg
    assert "CUSTOM_ADMIN_ENV_KEY" not in env_vars
    assert env_vars.get("SAFE_VAR") == "safe-value"

    # 2. All three leaking servers are marked unavailable (enabled=False) and cannot start with the token
    for srv_name in ("leak_env_srv", "leak_url_srv", "leak_args_srv"):
        assert by_name[srv_name].enabled is False
        srv_entry = mcp_client_cfg["servers"][srv_name]
        assert srv_entry.get("enabled") is False
        assert srv_entry.get("available") is False
        assert not srv_entry.get("url")
        assert not srv_entry.get("command")
        assert not srv_entry.get("args")

    # Clean server using DALTON_BRIDGE_TOKEN remains enabled and intact
    assert by_name["clean_mcp_srv"].enabled is True
    assert mcp_client_cfg["servers"]["clean_mcp_srv"]["url"].endswith(mcp_secret)

    # 3. Warnings logged item names (`CUSTOM_ADMIN_ENV_KEY`, `url`, `args[1]`) and never logged the secret value
    assert admin_secret not in caplog.text
    assert "CUSTOM_ADMIN_ENV_KEY" in caplog.text
    assert "url" in caplog.text
    assert "args[1]" in caplog.text

    # 4. When DALTON_BRIDGE_ADMIN_TOKEN is empty, the check is skipped
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "")
    clean_loaded = plugins_mod.bundled_mcp_servers()
    sandbox_stub._builtin_servers.return_value = clean_loaded
    env_when_unset = PTCSandbox._build_sandbox_env_vars(sandbox_stub, {})
    cfg_when_unset = gen.generate_client_config(clean_loaded)
    assert env_when_unset.get("SAFE_VAR") == "safe-value"
    assert all(s.enabled is True for s in clean_loaded)
    assert "enabled" not in cfg_when_unset["servers"]["clean_mcp_srv"]


def test_sandbox_env_vars_never_resolve_admin_token_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock the merged env-var loop in ``PTCSandbox._build_sandbox_env_vars``:
    an *unexpanded* ``${DALTON_BRIDGE_ADMIN_TOKEN}`` / ``${INTERNAL_SERVICE_TOKEN}``
    reference under any config key must never resolve into the sandbox env,
    while bridge URLs (``DALTON_RESEARCHDATA_URL`` etc.) still do — on both the
    Dalton oss path and upstream's platform-secret withholding path."""
    from ptc_agent.config.core import MCPServerConfig
    from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

    admin_secret = "admin-placeholder-secret"
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", admin_secret)
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "internal-platform-secret")
    monkeypatch.setenv("DALTON_RESEARCHDATA_URL", "http://host.docker.internal:8960")
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", "mcp-token-ok")

    servers = [
        MCPServerConfig(
            name="stdio_srv",
            transport="stdio",
            command="python3",
            env={
                "DALTON_RESEARCHDATA_URL": "${DALTON_RESEARCHDATA_URL}",
                "DALTON_BRIDGE_TOKEN": "${DALTON_BRIDGE_TOKEN}",
                "RENAMED_ADMIN": "${DALTON_BRIDGE_ADMIN_TOKEN}",
                "RENAMED_INTERNAL": "${INTERNAL_SERVICE_TOKEN}",
                "PLAIN_VALUE": "plain-ok",
            },
        ),
    ]
    class _TrustedProvider:
        # B3's builder gates the deployment env on provider trust; the stub
        # host is trusted so the token paths under test run.
        @staticmethod
        def trusted_host(config):
            return True

    sandbox_stub = create_autospec(PTCSandbox, instance=True)
    sandbox_stub.provider = _TrustedProvider()
    # The real builder delegates to _deployment_env_vars off the instance;
    # on the autospec stub that dispatch would hit a mock, so delegate back.
    sandbox_stub._deployment_env_vars.side_effect = (
        lambda bindings: PTCSandbox._deployment_env_vars(sandbox_stub, bindings)
    )
    sandbox_stub.config = SimpleNamespace(sandbox=SimpleNamespace(provider="docker"))
    sandbox_stub._token_file_path = "/home/workspace/.ginlix_token"
    sandbox_stub._builtin_servers.return_value = servers

    env_vars = PTCSandbox._build_sandbox_env_vars(sandbox_stub, {})

    assert admin_secret not in json.dumps(env_vars)
    assert "internal-platform-secret" not in json.dumps(env_vars)
    assert "RENAMED_ADMIN" not in env_vars
    assert "RENAMED_INTERNAL" not in env_vars
    assert env_vars["DALTON_RESEARCHDATA_URL"] == "http://host.docker.internal:8960"
    assert env_vars["DALTON_BRIDGE_TOKEN"] == "mcp-token-ok"
    assert env_vars["PLAIN_VALUE"] == "plain-ok"


def test_sandbox_env_and_mcp_config_exclude_internal_service_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`INTERNAL_SERVICE_TOKEN`'s value is scrubbed by value (whatever key it
    was expanded under) from sandbox env vars and from the generated MCP client
    config (url / command / args / headers), with the leaking server disabled.
    Each token is checked on its own: with only the internal token configured,
    its leaks are scrubbed while an unset admin token never disables anything."""
    import ptc_agent.config.plugins as plugins_mod
    from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
    from ptc_agent.core.tool_generator import ToolFunctionGenerator

    internal_secret = "internal-service-secret-do-not-leak"
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", internal_secret)
    # Phase 1 runs with the bridge admin token unset.
    monkeypatch.delenv("DALTON_BRIDGE_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", "mcp-token-allowed")

    test_plugins_dir = tmp_path / "plugins"
    bundle_dir = test_plugins_dir / "test_internal_token_guard"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "test_internal_token_guard",
                "version": "1.0.0",
                "description": "Test bundle for INTERNAL_SERVICE_TOKEN boundary scrubbing",
            }
        ),
        encoding="utf-8",
    )
    (bundle_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "leak_env_srv": {
                        "type": "stdio",
                        "command": "python3",
                        "args": ["env_server.py"],
                        "env": {
                            "RENAMED_INTERNAL": "${INTERNAL_SERVICE_TOKEN}",
                            "SAFE_VAR": "safe-value",
                        },
                    },
                    "leak_url_srv": {
                        "type": "streamable-http",
                        "url": "http://host.docker.internal:9000/mcp/${INTERNAL_SERVICE_TOKEN}",
                    },
                    "leak_headers_srv": {
                        "type": "streamable-http",
                        "url": "http://host.docker.internal:9001/mcp",
                        "headers": {"X-Internal": "Bearer ${INTERNAL_SERVICE_TOKEN}"},
                    },
                    "leak_args_srv": {
                        "type": "stdio",
                        "command": "python3",
                        "args": [
                            "args_server.py",
                            "--internal=${INTERNAL_SERVICE_TOKEN}",
                        ],
                    },
                    "clean_mcp_srv": {
                        "type": "streamable-http",
                        "url": "http://host.docker.internal:8960/mcp/${DALTON_BRIDGE_TOKEN}",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(plugins_mod, "BUNDLES_DIR", test_plugins_dir)

    # The real loader expands ${INTERNAL_SERVICE_TOKEN} into plaintext first.
    loaded_servers = plugins_mod.bundled_mcp_servers()
    by_name = {s.name: s for s in loaded_servers}
    assert by_name["leak_env_srv"].env["RENAMED_INTERNAL"] == internal_secret

    class _TrustedProvider:
        # B3's builder gates the deployment env on provider trust; the stub
        # host is trusted so the token paths under test run.
        @staticmethod
        def trusted_host(config):
            return True

    sandbox_stub = create_autospec(PTCSandbox, instance=True)
    sandbox_stub.provider = _TrustedProvider()
    sandbox_stub.config = SimpleNamespace(sandbox=SimpleNamespace(provider="docker"))
    sandbox_stub._token_file_path = "/home/workspace/.ginlix_token"
    sandbox_stub._builtin_servers.return_value = loaded_servers
    # The real builder delegates to _deployment_env_vars off the instance;
    # on the autospec stub that dispatch would hit a mock, so delegate back.
    sandbox_stub._deployment_env_vars.side_effect = (
        lambda bindings: PTCSandbox._deployment_env_vars(sandbox_stub, bindings)
    )

    env_vars = PTCSandbox._build_sandbox_env_vars(sandbox_stub, {})
    gen = ToolFunctionGenerator()
    mcp_client_cfg = gen.generate_client_config(loaded_servers)

    assert internal_secret not in json.dumps(env_vars)
    assert internal_secret not in json.dumps(mcp_client_cfg)
    assert "RENAMED_INTERNAL" not in env_vars
    assert env_vars.get("SAFE_VAR") == "safe-value"
    for srv_name in ("leak_env_srv", "leak_url_srv", "leak_headers_srv", "leak_args_srv"):
        assert by_name[srv_name].enabled is False
        srv_entry = mcp_client_cfg["servers"][srv_name]
        assert srv_entry.get("enabled") is False
        assert srv_entry.get("available") is False
    assert by_name["clean_mcp_srv"].enabled is True
    assert mcp_client_cfg["servers"]["clean_mcp_srv"]["url"].endswith("mcp-token-allowed")

    # Phase 2: with the admin token configured too, a server leaking either
    # token is disabled — each token scrubs independently of the other.
    admin_secret = "admin-secret-phase-two"
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", admin_secret)
    loaded_both = plugins_mod.bundled_mcp_servers()
    by_name_both = {s.name: s for s in loaded_both}
    sandbox_stub._builtin_servers.return_value = loaded_both
    env_both = PTCSandbox._build_sandbox_env_vars(sandbox_stub, {})
    cfg_both = ToolFunctionGenerator().generate_client_config(loaded_both)

    assert internal_secret not in json.dumps(env_both)
    assert admin_secret not in json.dumps(env_both)
    assert internal_secret not in json.dumps(cfg_both)
    assert admin_secret not in json.dumps(cfg_both)
    assert by_name_both["leak_env_srv"].enabled is False
    assert by_name_both["leak_url_srv"].enabled is False
    assert by_name_both["clean_mcp_srv"].enabled is True


def test_transformed_command_log_redacts_management_tokens(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The debug line that prints a stdio server's original args (already
    expanded to plaintext by the plugins loader) redacts both management
    tokens' values before the record is emitted. An earlier test's import
    chain may configure structlog to filter DEBUG globally, so this test
    re-enables DEBUG for a fresh, uncached logger and reads the stdout the
    default structlog factory writes."""
    import logging

    import structlog

    import src.ptc_agent.core.tool_generator as tg_mod
    from ptc_agent.config.core import MCPServerConfig

    admin_secret = "admin-secret-for-log-test"
    internal_secret = "internal-secret-for-log-test"
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", admin_secret)
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", internal_secret)
    monkeypatch.delenv("DALTON_BRIDGE_TOKEN", raising=False)

    server = MCPServerConfig(
        name="log_redact_srv",
        transport="stdio",
        command="uv",
        args=[
            "run",
            "python",
            "some_mcp_server.py",
            f"--admin={admin_secret}",
            f"--internal={internal_secret}",
        ],
    )

    saved_config = structlog.get_config()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        cache_logger_on_first_use=False,
    )
    monkeypatch.setattr(tg_mod, "logger", structlog.get_logger(tg_mod.__name__))
    try:
        tg_mod.ToolFunctionGenerator().generate_client_config([server])
    finally:
        structlog.configure(**saved_config)

    emitted = capsys.readouterr().out

    assert admin_secret not in emitted
    assert internal_secret not in emitted
    assert "--admin=***" in emitted
    assert "--internal=***" in emitted


@pytest.mark.asyncio
async def test_realtime_ops_jobs_and_archive_last_run_log_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify real-time ops jobs inspection (pg-backup + backend-watchdog, no ops-log-rotate),
    archive log tail slicing from the last `=== ` run header, and host schedule metadata.
    """
    import plistlib
    from datetime import datetime, timedelta

    pg_log = tmp_path / "dalton-pg-backup.log"
    pg_plist = tmp_path / "com.dalton.pg-backup.plist"
    wd_plist = tmp_path / "com.dalton.backend-watchdog.plist"
    wd_failures = tmp_path / ".watchdog_failures"
    archive_log = tmp_path / "dalton-conversation-archive.log"

    pg_log.write_text(
        "2026-10-25 04:00:01 ok 750M /Users/alice/Backups/dalton-pg-20261025-040000.dump\n"
        "2026-10-26 07:42:19 ok 784M /Users/alice/Backups/dalton-pg-20261026-074111.dump\n\n",
        encoding="utf-8",
    )
    pg_plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.dalton.pg-backup",
                "StartCalendarInterval": {"Hour": 4, "Minute": 30},
            }
        )
    )
    wd_plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.dalton.backend-watchdog",
                "StartInterval": 60,
            }
        )
    )
    wd_failures.write_text("0\n", encoding="utf-8")

    monkeypatch.setenv("DALTON_PG_BACKUP_LOG_FILE", str(pg_log))
    monkeypatch.setenv("DALTON_PG_BACKUP_PLIST_FILE", str(pg_plist))
    monkeypatch.setenv("DALTON_BACKEND_WATCHDOG_PLIST_FILE", str(wd_plist))
    monkeypatch.setenv("DALTON_WATCHDOG_FAILURES_FILE", str(wd_failures))
    monkeypatch.setenv("DALTON_ARCHIVE_LOG_FILE", str(archive_log))

    cfg = bridge_mod.BridgeConfig()
    bridge = bridge_mod.ResearchDataBridge(cfg)

    real_subprocess_run = subprocess.run
    expected_uid = os.getuid()
    expected_cmds = {
        ("launchctl", "print", f"gui/{expected_uid}/com.dalton.pg-backup"),
        ("launchctl", "print", f"gui/{expected_uid}/com.dalton.backend-watchdog"),
    }

    def _fake_subprocess_run(
        cmd: list[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        timeout: float | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert tuple(cmd) in expected_cmds
        assert timeout == 2.0
        assert capture_output is True
        assert text is True
        assert check is False
        target = cmd[2]
        if target.endswith("/com.dalton.pg-backup"):
            return subprocess.CompletedProcess(cmd, 0, stdout="state = waiting\n", stderr="")
        return subprocess.CompletedProcess(cmd, 113, stdout="", stderr="Could not find service")

    monkeypatch.setattr(
        bridge_mod.subprocess,
        "run",
        create_autospec(real_subprocess_run, side_effect=_fake_subprocess_run),
    )

    code, payload = await bridge.handle_get_ops_jobs()
    assert code == 200
    assert payload["ok"] is True
    jobs = payload["jobs"]
    assert [j["key"] for j in jobs] == ["ops-db-backup", "ops-backend-watchdog"]

    db_job = jobs[0]
    assert db_job["status"] == "on"
    assert db_job["last_result"] == "成功（784M）"
    assert "/Users/alice" not in str(db_job["last_result"])
    parsed_db_last = datetime.fromisoformat(db_job["last_run_at"])
    assert parsed_db_last.tzinfo is not None
    assert (parsed_db_last.year, parsed_db_last.month, parsed_db_last.day) == (2026, 10, 26)
    assert (parsed_db_last.hour, parsed_db_last.minute, parsed_db_last.second) == (7, 42, 19)
    parsed_db_next = datetime.fromisoformat(db_job["next_run_at"])
    assert parsed_db_next.tzinfo is not None
    assert (parsed_db_next.hour, parsed_db_next.minute) == (4, 30)

    wd_job = jobs[1]
    assert wd_job["status"] == "off"
    assert wd_job["last_result"] == "健康"
    parsed_wd_last = datetime.fromisoformat(wd_job["last_run_at"])
    parsed_wd_next = datetime.fromisoformat(wd_job["next_run_at"])
    assert parsed_wd_next - parsed_wd_last == timedelta(seconds=60)

    # Check failure formatting for pg-backup and backend-watchdog, plus timeout -> unknown
    pg_log.write_text(
        "2026-10-26 08:00:00 fail 0B /Users/alice/Backups/dalton-pg-failed.dump\n",
        encoding="utf-8",
    )
    wd_failures.write_text("3\n", encoding="utf-8")

    def _timeout_subprocess_run(
        cmd: list[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        timeout: float | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert tuple(cmd) in expected_cmds
        assert timeout == 2.0
        assert capture_output is True
        assert text is True
        assert check is False
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=2.0)

    monkeypatch.setattr(
        bridge_mod.subprocess,
        "run",
        create_autospec(real_subprocess_run, side_effect=_timeout_subprocess_run),
    )

    _, payload_fail = await bridge.handle_get_ops_jobs()
    db_fail, wd_fail = payload_fail["jobs"]
    assert db_fail["status"] == "unknown"
    assert db_fail["last_result"] == "失败"
    assert wd_fail["status"] == "unknown"
    assert wd_fail["last_result"] == "连续失败 3 次"

    # Non-negative integer check on watchdog failures: negative or arbitrary text sets last_result=None
    for invalid_content in ("-1\n", "corrupted /Users/alice/secret\n", ""):
        wd_failures.write_text(invalid_content, encoding="utf-8")
        _, payload_invalid = await bridge.handle_get_ops_jobs()
        assert payload_invalid["jobs"][1]["last_result"] is None

    # Verify _read_sanitized_archive_log_tail slices from the last `=== ` run header (not `=== exit`)
    # and ignores stderr file when stdout has a `=== ` boundary
    archive_err_log = tmp_path / "dalton-conversation-archive.err.log"
    cfg.archive_err_log_file = archive_err_log
    archive_err_log.write_text("STALE_STDERR_FROM_OTHER_RUN\n", encoding="utf-8")
    archive_log.write_text(
        "=== 2026-10-25 21:15:00 EDT attempt 1\n"
        "OLD_RUN_ERROR_LINE_SHOULD_NOT_APPEAR\n"
        "=== exit 1\n"
        "=== 2026-10-26 21:15:00 EDT attempt 1\n"
        "latest run line 1\n"
        "=== exit 0\n",
        encoding="utf-8",
    )
    tail = bridge._read_sanitized_archive_log_tail()
    assert "OLD_RUN_ERROR_LINE_SHOULD_NOT_APPEAR" not in tail
    assert "STALE_STDERR_FROM_OTHER_RUN" not in tail
    assert "=== 2026-10-25 21:15:00 EDT attempt 1" not in tail
    assert "=== 2026-10-26 21:15:00 EDT attempt 1" in tail
    assert "latest run line 1" in tail
    assert "=== exit 0" in tail

    # When stdout has no `=== ` boundary (e.g. empty), stderr is used as fallback
    archive_log.write_text("", encoding="utf-8")
    fallback_tail = bridge._read_sanitized_archive_log_tail()
    assert "STALE_STDERR_FROM_OTHER_RUN" in fallback_tail

    # Verify _compute_next_calendar_interval_iso across DST boundary (2026-10-31 -> 2026-11-01 04:30 in America/New_York is -05:00)
    from zoneinfo import ZoneInfo

    ny_tz = ZoneInfo("America/New_York")
    fake_localtime = tmp_path / "localtime"
    fake_localtime.symlink_to("/var/db/timezone/zoneinfo/America/New_York")
    now_before_dst_end = datetime(2026, 10, 31, 12, 0, 0, tzinfo=ny_tz)
    next_iso_dst = bridge_mod.ResearchDataBridge._compute_next_calendar_interval_iso(
        {"StartCalendarInterval": {"Hour": 4, "Minute": 30}},
        now=now_before_dst_end,
        localtime_path=fake_localtime,
    )
    assert next_iso_dst == "2026-11-01T04:30:00-05:00"

    # Weekday / Day / Month in StartCalendarInterval returns None
    for unsupported_key in ("Weekday", "Day", "Month"):
        assert (
            bridge_mod.ResearchDataBridge._compute_next_calendar_interval_iso(
                {"StartCalendarInterval": {"Hour": 4, "Minute": 30, unsupported_key: 1}},
                now=now_before_dst_end,
                localtime_path=fake_localtime,
            )
            is None
        )

    # Verify _compute_next_archive_run_at reads archive plist (and returns None when disabled)
    archive_plist = tmp_path / "com.dalton.conversation-archive.plist"
    archive_plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.dalton.conversation-archive",
                "StartCalendarInterval": {"Hour": 21, "Minute": 15},
            }
        )
    )
    cfg.archive_plist_file = archive_plist
    assert bridge._compute_next_archive_run_at(enabled=False, now=now_before_dst_end) is None
    archive_next_iso = bridge._compute_next_archive_run_at(enabled=True, now=now_before_dst_end)
    assert archive_next_iso is not None
    parsed_archive_next = datetime.fromisoformat(archive_next_iso)
    assert (parsed_archive_next.hour, parsed_archive_next.minute) == (21, 15)

    # Verify _fetch_host_conversation_archive_job has schedule=None and no hardcoded 21:15 in description
    monkeypatch.setattr(
        system_jobs_mod,
        "_call_host_bridge",
        create_autospec(
            system_jobs_mod._call_host_bridge,
            return_value=(
                200,
                {
                    "ok": True,
                    "enabled": True,
                    "status": "on",
                    "last_run_at": "2026-10-26T01:15:00+00:00",
                    "exit_code": 0,
                    "last_result": "成功 (exit_code=0)",
                    "log_tail": tail,
                    "next_run_at": "2026-10-27T01:15:00+00:00",
                    "running": False,
                },
            ),
        ),
    )
    host_item = await system_jobs_mod._fetch_host_conversation_archive_job()
    assert host_item["schedule"] is None
    assert "21:15" not in host_item["description"]


@pytest.mark.asyncio
async def test_archive_disconnects_only_owned_builtin_mcp_registry_and_archive_daily_retry(
    tmp_path: Path,
) -> None:
    """Verify `_disconnect_workspace_manager_mcp` only disconnects owned `_builtin_mcp_registry`
    (never borrowed `_builtin_mcp_registry` or composite `mcp_registry`), and `archive_daily.sh`
    retries once on exit code 3 with `=== <time> attempt N` / `=== exit <rc>` boundaries.
    """
    from types import SimpleNamespace

    import scripts.dalton.conversation_archive as archive_mod
    from src.ptc_agent.core.mcp_registry import MCPRegistry
    from src.ptc_agent.core.session import Session

    sess_owned = create_autospec(Session, instance=True)
    sess_owned._builtin_mcp_registry = create_autospec(MCPRegistry, instance=True)
    sess_owned._owns_mcp_registry = True
    sess_owned.mcp_registry = create_autospec(MCPRegistry, instance=True)

    sess_borrowed = create_autospec(Session, instance=True)
    sess_borrowed._builtin_mcp_registry = create_autospec(MCPRegistry, instance=True)
    sess_borrowed._owns_mcp_registry = False
    sess_borrowed.mcp_registry = create_autospec(MCPRegistry, instance=True)

    wm_stub = SimpleNamespace(
        _machines={
            "ws-owned": SimpleNamespace(session=sess_owned),
            "ws-borrowed": SimpleNamespace(session=sess_borrowed),
        }
    )

    await archive_mod._disconnect_workspace_manager_mcp(wm_stub, timeout_s=5.0)

    sess_owned._builtin_mcp_registry.disconnect_all.assert_awaited_once()
    sess_borrowed._builtin_mcp_registry.disconnect_all.assert_not_called()
    sess_owned.mcp_registry.disconnect_all.assert_not_called()
    sess_borrowed.mcp_registry.disconnect_all.assert_not_called()
    sess_owned.cleanup.assert_not_called()
    sess_owned.stop.assert_not_called()
    sess_borrowed.cleanup.assert_not_called()
    sess_borrowed.stop.assert_not_called()

    # Verify archive_daily.sh retries on rc=3 (attempt 1 -> rc 3, attempt 2 -> rc 0) and writes final status
    script_path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "dalton"
        / "launchd"
        / "archive_daily.sh"
    )
    state_file = tmp_path / "state.json"
    status_file = tmp_path / "status.json"
    pid_file = tmp_path / "archive.pid"
    count_file = tmp_path / "attempts.txt"
    fake_py_script = tmp_path / "fake_archive_retry.py"
    state_file.write_text('{"enabled": true}\n', encoding="utf-8")
    fake_py_script.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        f"p = Path({str(count_file)!r})\n"
        "n = int(p.read_text()) + 1 if p.is_file() else 1\n"
        "p.write_text(str(n))\n"
        "print(f'embed={os.environ.get(\"DALTON_BRIDGE_EMBED_URL\")} no_proxy={os.environ.get(\"no_proxy\")}')\n"
        "sys.exit(3 if n == 1 else 0)\n",
        encoding="utf-8",
    )
    res = await asyncio.to_thread(
        subprocess.run,
        ["/bin/bash", str(script_path)],
        env={
            **os.environ,
            "DALTON_ARCHIVE_STATE_FILE": str(state_file),
            "DALTON_ARCHIVE_STATUS_FILE": str(status_file),
            "DALTON_ARCHIVE_PID_FILE": str(pid_file),
            "DALTON_CONVERSATION_ARCHIVE_PY": str(fake_py_script),
            "DALTON_ARCHIVE_RETRY_SLEEP_S": "0",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res.returncode == 0
    assert res.stdout.count("=== ") == 2  # one run header and one final exit line
    assert "--- attempt 1 exit 3" in res.stdout
    assert "--- attempt 2 exit 0" in res.stdout
    assert res.stdout.rstrip().endswith("=== exit 0")
    assert "embed=http://127.0.0.1:8960/embed" in res.stdout
    assert "host.docker.internal" in res.stdout
    saved_status = json.loads(status_file.read_text(encoding="utf-8"))
    assert saved_status["exit_code"] == 0

    # Switched off while waiting to retry: attempt 2 does not run, final rc stays 3.
    count_file.unlink()
    fake_py_script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"p = Path({str(count_file)!r})\n"
        "n = int(p.read_text()) + 1 if p.is_file() else 1\n"
        "p.write_text(str(n))\n"
        f"Path({str(state_file)!r}).write_text('{{\"enabled\": false}}')\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    state_file.write_text('{"enabled": true}\n', encoding="utf-8")
    res_off = await asyncio.to_thread(
        subprocess.run,
        ["/bin/bash", str(script_path)],
        env={
            **os.environ,
            "DALTON_ARCHIVE_STATE_FILE": str(state_file),
            "DALTON_ARCHIVE_STATUS_FILE": str(status_file),
            "DALTON_ARCHIVE_PID_FILE": str(pid_file),
            "DALTON_CONVERSATION_ARCHIVE_PY": str(fake_py_script),
            "DALTON_ARCHIVE_RETRY_SLEEP_S": "0",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_off.returncode == 3
    assert count_file.read_text() == "1"
    assert "retry skipped: archive was switched off" in res_off.stdout
    assert json.loads(status_file.read_text(encoding="utf-8"))["exit_code"] == 3





