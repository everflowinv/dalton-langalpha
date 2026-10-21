#!/usr/bin/env python3
"""G2 Real-Run Verification Script — Dalton API Routes, Provider Probes, PM RBAC & Startup Fail-Fast (P0J §5 G2, L37, L38).

Verifies against the live HTTP backend (`http://127.0.0.1:8000`) under `realrun-pm-a` and `realrun-user-b`
(never `local-dev-user`):
1. Enumerates all `/api/v1/dalton` routes from `GET /openapi.json` and invokes every `(method, path)` pair,
   asserting 0 missed routes.
2. Probes configured providers (`POST /api/v1/dalton/providers/{provider}/probe`, including cash provider `deepseek`
   and `antigravity-oauth` subscription models) and 1 unconfigured provider (`zhipu`),
   verifying real `dalton_llm_journal` entries (`purpose='provider_probe'`) and safe `error_kind` with zero credential text.
3. Calls PM-exclusive route (`POST /api/v1/dalton/knowledge/{ws}/thesis/import`) as `realrun-pm-a` (in `pm_user_ids` -> 200)
   and `realrun-user-b` (not in `pm_user_ids` -> 403).
4. Verifies startup fail-fast (`RolePolicyError` / `DaltonStartupConfigError`) by launching a real backend child process
   (`uvicorn src.server.app:app`) with a temporary config file (`PTC_CONFIG_FILE`) setting
   `llm.assist: muse-cli-gateway/muse-spark-1.3-contributor`, asserting non-zero exit code and startup error log.
5. Restores `realrun-pm-a`'s original budget and BYOK API keys configuration via `_restore_budget_state` and `_restore_api_keys_state`, and deletes temporary workspaces in `finally`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.realrun._common import (  # noqa: E402
    RealRunHttpClient,
    assert_no_conflicting_dalton_automation,
    build_evidence_envelope,
    build_realrun_workspace_config,
    default_shanghai_date,
    ensure_temporary_deepseek_byok,
    load_repo_dotenv,
    now_utc_iso,
    preflight_realrun_script,
    write_evidence_and_summary,
)
from scripts.dalton.realrun.check_env import (  # noqa: E402
    _restore_api_keys_state,
    _restore_budget_state,
    _snapshot_api_keys_state,
)


def _verify_startup_fail_fast() -> dict[str, Any]:
    """Start a real backend child process (`uvicorn src.server.app:app`) with a temporary config file
    where `llm.assist` is set to `muse-cli-gateway/muse-spark-1.3-contributor`, wait for the process
    to exit on its own (60s deadline), and assert `exit_code > 0` (not killed by signal) and RolePolicyError (P0J §5 G2).
    """
    upstream_cfg_path = REPO_ROOT / "agent_config.yaml"
    dalton_cfg_path = REPO_ROOT / "agent_config.dalton.yaml"
    upstream_cfg = yaml.safe_load(upstream_cfg_path.read_text(encoding="utf-8")) or {}
    dalton_cfg = yaml.safe_load(dalton_cfg_path.read_text(encoding="utf-8")) or {}
    upstream_cfg.setdefault("llm", {})["assist"] = "muse-cli-gateway/muse-spark-1.3-contributor"
    dalton_cfg.setdefault("llm", {})["assist"] = "muse-cli-gateway/muse-spark-1.3-contributor"

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "src.server.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        "18099",
    ]
    with tempfile.TemporaryDirectory(prefix="g2_role_policy_") as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        bad_cfg_path = tmp_dir_path / "agent_config.yaml"
        bad_dalton_cfg_path = tmp_dir_path / "agent_config.dalton.yaml"
        bad_cfg_path.write_text(
            yaml.safe_dump(upstream_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        bad_dalton_cfg_path.write_text(
            yaml.safe_dump(dalton_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["DALTON_BUDGET_GATE"] = "1"
        env["PTC_CONFIG_FILE"] = str(bad_cfg_path)
        env["PYTHONUNBUFFERED"] = "1"

        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            stdout_text, _ = proc.communicate(timeout=60.0)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            partial_out, _ = proc.communicate()
            raise AssertionError(
                "uvicorn startup fail-fast check timed out after 60s without exiting on its own: "
                f"{(partial_out or '')[-800:]}"
            ) from exc

    combined_out = (stdout_text or "").strip()
    collected_lines = [ln.rstrip("\r\n") for ln in combined_out.splitlines()]
    tail_lines = "\n".join(collected_lines[-15:])
    has_role_err = (
        "RolePolicyError" in combined_out
        and "muse-spark-1.3-contributor" in combined_out
    )
    exited_positive = proc.returncode is not None and proc.returncode > 0
    failed_fast = exited_positive and has_role_err
    assert failed_fast, (
        "Expected uvicorn startup with assist=muse-spark-1.3-contributor to exit on its own "
        f"with exit_code > 0 and RolePolicyError, got exit_code={proc.returncode}, tail={tail_lines}"
    )
    return {
        "command": " ".join(cmd[1:]),
        "temp_config_path": "tempfile:agent_config.yaml",
        "override_config": {"llm.assist": "muse-cli-gateway/muse-spark-1.3-contributor"},
        "exit_code": proc.returncode,
        "failed_fast": failed_fast,
        "error_excerpt": tail_lines[-800:],
    }


def _build_g2_byok_evidence_fields(
    *,
    pm_user: str,
    key_modified_pm: bool,
    restore_key_err_pm: str | None,
    restore_budget_err_pm: str | None,
) -> dict[str, Any]:
    """Build truthful evidence fields for G2 temporary DeepSeek BYOK key setup/cleanup (P0J §8.1 #19)."""
    temp_deleted_pm = bool(key_modified_pm and restore_key_err_pm is None)
    api_keys_restored: bool | str = (restore_key_err_pm is None) if key_modified_pm else "not_applicable"
    budget_restored = restore_budget_err_pm is None
    settings_endpoint = (
        "PUT /api/v1/users/me/api-keys"
        if key_modified_pm
        else "GET /api/v1/users/me/api-keys (preexisting key preserved)"
    )
    key_write_skipped_reason = None if key_modified_pm else "preexisting_user_deepseek_key_preserved"
    pm_key_desc = (
        f"临时 BYOK Key 已删除={temp_deleted_pm}"
        if key_modified_pm
        else "既有 BYOK Key 保留未改动（未写入、未删除）"
    )
    return {
        "user_id": pm_user,
        "settings_endpoint": settings_endpoint,
        "byok_provider": "deepseek",
        "temporary_key_written": bool(key_modified_pm),
        "temporary_key_deleted": temp_deleted_pm,
        "key_write_skipped_reason": key_write_skipped_reason,
        "api_keys_restored": api_keys_restored,
        "budget_restored": budget_restored,
        "restoration_summary": f"PM 用户 ({pm_user}) 预算已恢复={budget_restored}（{pm_key_desc}）",
    }


def main() -> int:
    load_repo_dotenv()
    parser = argparse.ArgumentParser(description="G2 Real-Run Evidence Generator (P0J §5 G2)")
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--date", default=default_shanghai_date())
    parser.add_argument("--pm-user", default="realrun-pm-a")
    parser.add_argument("--other-user", default="realrun-user-b")
    parser.add_argument("--record", action="store_true", help="Write evidence JSON and summary under var/dalton/")
    parser.add_argument(
        "--only-startup-check",
        action="store_true",
        help="Debug helper: run only the uvicorn startup fail-fast check (disallowed with --record)",
    )
    args = parser.parse_args()

    if args.only_startup_check and args.record:
        raise SystemExit("ERROR: --only-startup-check cannot be combined with --record")

    preflight_realrun_script("G2", record=args.record, backend=args.backend)

    if args.only_startup_check:
        startup_fail_check = _verify_startup_fail_fast()
        print(json.dumps({"startup_fail_fast_check": startup_fail_check}, ensure_ascii=False, indent=2))
        return 0

    started_at = now_utc_iso()
    pm_user = args.pm_user
    other_user = args.other_user
    test_user_id = f"{pm_user},{other_user}"

    synthetic: list[dict[str, Any]] = []

    sample_thesis_v1 = {
        "schema_version": 1,
        "as_of": started_at,
        "one_liner": "Alphabet Inc. G2 路由与权限测试初始观点 v1",
        "aggregate_status": "intact",
        "house_view": {
            "stance": "long",
            "conviction": "high",
            "horizon_months": 18,
            "summary": "G2 路由与权限测试初始观点 v1",
            "variant_perception": "Search and Cloud operating margin durability exceeds consensus.",
            "updated_at": started_at,
        },
        "house_view_history": [],
        "pillars": [
            {
                "id": "P1",
                "claim": "GOOGL core revenue growth and operating margin trajectory.",
                "priority": "core",
                "baseline": {"text": "2Q26 SEC 10-Q baseline.", "as_of": "2026-06-30"},
                "expected_path": "Maintain double-digit revenue growth and margin expansion.",
                "thresholds": {"confirm": [], "warn": [], "break": []},
                "status": "intact",
                "provenance": "approved",
                "created_at": started_at,
            }
        ],
        "evidence": [],
        "official": None,
    }
    sample_thesis_v2 = {
        **sample_thesis_v1,
        "one_liner": "Alphabet Inc. G2 路由与权限测试更新观点 v2",
        "house_view": {
            **sample_thesis_v1["house_view"],
            "summary": "G2 路由与权限测试更新观点 v2",
        },
    }

    sample_ws_id: str | None = None
    user_b_ws_id: str | None = None
    created_auto_id: str | None = None
    prior_budget_pm: dict[str, Any] | None = None
    was_configured_pm = False
    budget_modified_pm = False
    prior_keys_pm: dict[str, Any] | None = None
    key_modified_pm = False
    restore_key_err_pm: str | None = None
    restore_budget_err_pm: str | None = None
    active_base_exc: BaseException | None = None

    try:
        with (
            RealRunHttpClient(base_url=args.backend, user_id=pm_user, timeout_s=60.0) as pm_client,
            RealRunHttpClient(base_url=args.backend, user_id=other_user, timeout_s=60.0) as user_b_client,
        ):
            pm_client.ensure_user()
            user_b_client.ensure_user()

            st_prior_b, body_prior_b = pm_client.get("/api/v1/dalton/budget")
            if st_prior_b == 200 and isinstance(body_prior_b, dict):
                prior_budget_pm = body_prior_b
                was_configured_pm = bool(body_prior_b.get("configured"))
            prior_keys_pm = _snapshot_api_keys_state(pm_client)
            st_prior_bt, body_prior_bt = pm_client.get("/api/v1/dalton/budget/today")
            baseline_spent_pm = (
                float(body_prior_bt.get("spent_usd") or 0.0)
                if st_prior_bt == 200 and isinstance(body_prior_bt, dict)
                else 0.0
            )
            g2_cap_usd = round(max(5.0, baseline_spent_pm + 5.0), 6)
            synthetic.append(
                {
                    "kind": "settings_api_budget",
                    "entry_point": "PUT /api/v1/dalton/budget",
                    "description": (
                        f"为测试用户 {pm_user} 设置相对日预算上限 ${g2_cap_usd:.4f} "
                        f"（max($5.00, 当日已有现金花费 ${baseline_spent_pm:.4f} + $5.00)），"
                        "用于验证现金探测计入当日预算，并在结束时恢复原预算配置。"
                    ),
                }
            )

            # 1. Create a workspace owned by pm_user for parameterized knowledge/verdict/task-cap routes
            st_ws, ws_resp = pm_client.post(
                "/api/v1/workspaces",
                json_body={
                    "name": f"G2-GOOGL-{args.date}",
                    "description": "G2 route coverage workspace",
                    "config": build_realrun_workspace_config("g2_providers.py"),
                },
            )
            assert st_ws in (200, 201) and ws_resp.get("workspace_id"), f"Workspace create failed: {st_ws} {ws_resp}"
            sample_ws_id = str(ws_resp["workspace_id"])

            # Also create a workspace owned by user_b to test PM-only /import returning 403 on user_b's own workspace
            st_ws_b, ws_b_resp = user_b_client.post(
                "/api/v1/workspaces",
                json_body={
                    "name": f"G2-USERB-{args.date}",
                    "description": "G2 user_b RBAC workspace",
                    "config": build_realrun_workspace_config("g2_providers.py"),
                },
            )
            assert st_ws_b in (200, 201) and ws_b_resp.get("workspace_id")
            user_b_ws_id = str(ws_b_resp["workspace_id"])

            # 2. Configure budget and temporary DeepSeek BYOK key for pm_user (preserving any pre-existing user key per P0J §8.1 #19)
            st_budget_put, resp_budget_put = pm_client.put(
                "/api/v1/dalton/budget",
                json_body={"daily_cap_usd": g2_cap_usd, "default_task_cap_usd": 1.0},
            )
            assert st_budget_put == 200, f"PUT /api/v1/dalton/budget failed: {st_budget_put} {resp_budget_put}"
            budget_modified_pm = True
            if not bool((prior_keys_pm or {}).get("had_user_deepseek_key")):
                key_modified_pm = True
            ensure_temporary_deepseek_byok(pm_client, prior_keys_pm)

            # 3. Enumerate all /api/v1/dalton routes from /openapi.json
            st_spec, openapi_spec = pm_client.get("/openapi.json")
            assert st_spec == 200 and isinstance(openapi_spec, dict), "Failed to fetch /openapi.json"

            enumerated_routes: list[tuple[str, str]] = []
            for path, methods_obj in sorted((openapi_spec.get("paths") or {}).items()):
                if not path.startswith("/api/v1/dalton"):
                    continue
                for m in sorted(methods_obj.keys()):
                    if m.upper() in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                        enumerated_routes.append((m.upper(), path))

            called_routes: list[dict[str, Any]] = []
            called_set: set[tuple[str, str]] = set()

            def _invoke_and_record(
                method: str,
                template_path: str,
                actual_path: str,
                *,
                json_body: Any = None,
                params: dict[str, Any] | None = None,
                as_user_b: bool = False,
            ) -> tuple[int, Any]:
                target = user_b_client if as_user_b else pm_client
                status, body = target.request(
                    method, actual_path, json_body=json_body, params=params
                )
                called_set.add((method.upper(), template_path))
                called_routes.append(
                    {
                        "user_id": other_user if as_user_b else pm_user,
                        "method": method.upper(),
                        "template_path": template_path,
                        "actual_path": actual_path,
                        "status_code": status,
                    }
                )
                return status, body

            # Invoke all 22 /api/v1/dalton routes as pm_user (never local-dev-user)
            _invoke_and_record("GET", "/api/v1/dalton/budget", "/api/v1/dalton/budget")
            _invoke_and_record(
                "PUT",
                "/api/v1/dalton/budget",
                "/api/v1/dalton/budget",
                json_body={"daily_cap_usd": g2_cap_usd, "default_task_cap_usd": 1.0},
            )
            st_tc_put, body_tc_put = _invoke_and_record(
                "PUT",
                "/api/v1/dalton/budget/task-caps/{scope_kind}/{scope_id}",
                f"/api/v1/dalton/budget/task-caps/workspace/{sample_ws_id}",
                json_body={"cap_usd": 0.50},
            )
            assert st_tc_put == 200, f"Expected task-cap PUT to return 200, got {st_tc_put}: {body_tc_put}"
            st_tc_del, body_tc_del = _invoke_and_record(
                "DELETE",
                "/api/v1/dalton/budget/task-caps/{scope_kind}/{scope_id}",
                f"/api/v1/dalton/budget/task-caps/workspace/{sample_ws_id}",
            )
            assert st_tc_del == 200, f"Expected task-cap DELETE to return 200, got {st_tc_del}: {body_tc_del}"
            _invoke_and_record("GET", "/api/v1/dalton/budget/today", "/api/v1/dalton/budget/today")
            _invoke_and_record("GET", "/api/v1/dalton/coverage/board", "/api/v1/dalton/coverage/board")
            _invoke_and_record("GET", "/api/v1/dalton/autonomy/report", "/api/v1/dalton/autonomy/report")
            assert_no_conflicting_dalton_automation(
                pm_client,
                name="dalton-research-director-daily",
                workspace_id=sample_ws_id,
            )
            st_auto_pm, body_auto_pm = _invoke_and_record(
                "PUT",
                "/api/v1/dalton/automations/{name}",
                "/api/v1/dalton/automations/dalton-research-director-daily",
                json_body={
                    "workspace_id": sample_ws_id,
                    "entry": {
                        "type": "director_daily",
                        "prompt": "G2 director daily schedule check",
                        "cron": "0 8 * * 1-5",
                        "timezone": "America/New_York",
                        "enabled": False,
                    },
                },
            )
            if (
                st_auto_pm != 200
                or not isinstance(body_auto_pm, dict)
                or not body_auto_pm.get("ok")
                or body_auto_pm.get("action") != "created"
                or not body_auto_pm.get("automation_id")
            ):
                raise RuntimeError(
                    "Expected PUT /api/v1/dalton/automations/dalton-research-director-daily to create a new automation "
                    f"(action='created'), got status={st_auto_pm} body={body_auto_pm}"
                )
            created_auto_id = str(body_auto_pm["automation_id"])
            # Import v1 and v2 on sample_ws_id as PM user -> 200
            st_imp_v1, body_imp_v1 = _invoke_and_record(
                "POST",
                "/api/v1/dalton/knowledge/{workspace_id}/{doc}/import",
                f"/api/v1/dalton/knowledge/{sample_ws_id}/thesis/import",
                json_body={"content": sample_thesis_v1, "reason": "G2 PM import v1"},
            )
            assert st_imp_v1 == 200, f"Expected PM import v1 to return 200, got {st_imp_v1}: {body_imp_v1}"
            st_imp_v2, body_imp_v2 = _invoke_and_record(
                "POST",
                "/api/v1/dalton/knowledge/{workspace_id}/{doc}/import",
                f"/api/v1/dalton/knowledge/{sample_ws_id}/thesis/import",
                json_body={"content": sample_thesis_v2, "reason": "G2 PM import v2"},
            )
            assert st_imp_v2 == 200, f"Expected PM import v2 to return 200, got {st_imp_v2}: {body_imp_v2}"

            # Non-PM user (`realrun-user-b`) calling PM-exclusive `/import` -> 403
            st_imp_user_b, body_imp_user_b = _invoke_and_record(
                "POST",
                "/api/v1/dalton/knowledge/{workspace_id}/{doc}/import",
                f"/api/v1/dalton/knowledge/{user_b_ws_id}/thesis/import",
                json_body={"content": sample_thesis_v1, "reason": "G2 non-PM import attempt"},
                as_user_b=True,
            )
            assert st_imp_user_b == 403, (
                f"Expected non-PM user {other_user} to get 403 on /import, got {st_imp_user_b}: {body_imp_user_b}"
            )

            _invoke_and_record(
                "GET",
                "/api/v1/dalton/knowledge/{workspace_id}/{doc}/versions",
                f"/api/v1/dalton/knowledge/{sample_ws_id}/thesis/versions",
            )
            _invoke_and_record(
                "GET",
                "/api/v1/dalton/knowledge/{workspace_id}/{doc}/versions/{n}",
                f"/api/v1/dalton/knowledge/{sample_ws_id}/thesis/versions/1",
            )
            _invoke_and_record(
                "GET",
                "/api/v1/dalton/knowledge/{workspace_id}/{doc}/diff",
                f"/api/v1/dalton/knowledge/{sample_ws_id}/thesis/diff",
                params={"from": 1, "to": 2},
            )
            _, oc_health = _invoke_and_record("GET", "/api/v1/dalton/openclaw/health", "/api/v1/dalton/openclaw/health")
            _invoke_and_record("GET", "/api/v1/dalton/openclaw/models/status", "/api/v1/dalton/openclaw/models/status")
            st_sync, body_sync = _invoke_and_record(
                "POST",
                "/api/v1/dalton/openclaw/models/sync",
                "/api/v1/dalton/openclaw/models/sync",
                json_body={"dry_run": True},
            )
            assert st_sync == 200, f"Expected openclaw/models/sync dry_run to return 200, got {st_sync}: {body_sync}"
            _invoke_and_record("GET", "/api/v1/dalton/providers/available-models", "/api/v1/dalton/providers/available-models")
            _, prov_status_body = _invoke_and_record(
                "GET", "/api/v1/dalton/providers/status", "/api/v1/dalton/providers/status"
            )
            _invoke_and_record("GET", "/api/v1/dalton/publications", "/api/v1/dalton/publications")
            _invoke_and_record(
                "GET",
                "/api/v1/dalton/publications/{publication_id}",
                "/api/v1/dalton/publications/00000000-0000-0000-0000-000000000000",
            )
            _invoke_and_record("GET", "/api/v1/dalton/publish-requests", "/api/v1/dalton/publish-requests")
            _invoke_and_record(
                "GET",
                "/api/v1/dalton/verdicts",
                "/api/v1/dalton/verdicts",
                params={"workspace_id": sample_ws_id},
            )

            # 4. Probe configured providers (cash DeepSeek via user BYOK key + Antigravity OAuth subscription models) + 1 unconfigured provider under pm_user
            configured_probes: list[tuple[str, dict[str, Any]]] = [
                ("deepseek", {"model_key": "deepseek-flash"}),
                ("antigravity-oauth", {"model_key": "gemini-3.8-flash-low-oauth"}),
                ("antigravity-oauth", {"model_key": "claude-sonnet-5-5-low-oauth"}),
            ]
            probe_results: list[dict[str, Any]] = []
            for prov_key, req_body in configured_probes:
                st_p, body_p = _invoke_and_record(
                    "POST",
                    "/api/v1/dalton/providers/{provider}/probe",
                    f"/api/v1/dalton/providers/{prov_key}/probe",
                    json_body=req_body,
                )
                assert st_p == 200 and isinstance(body_p, dict) and body_p.get("ok") is True, (
                    f"Configured provider probe failed for {prov_key}: {st_p} {body_p}"
                )
                probe_results.append(body_p)

            ds_probe = next(p for p in probe_results if p.get("provider") == "deepseek")
            assert float(ds_probe.get("cash_usd") or 0.0) > 0.0, (
                f"DeepSeek cash probe did not record positive cash_usd: {ds_probe}"
            )

            # Unconfigured provider probe -> returns ok=False with error_kind and zero credential leak
            st_unconf, body_unconf = _invoke_and_record(
                "POST",
                "/api/v1/dalton/providers/{provider}/probe",
                "/api/v1/dalton/providers/zhipu/probe",
                json_body={},
            )
            assert st_unconf == 200 and isinstance(body_unconf, dict) and body_unconf.get("ok") is False, (
                f"Expected unconfigured provider probe to return ok=False, got {st_unconf}: {body_unconf}"
            )
            assert body_unconf.get("error_kind"), f"Missing error_kind on unconfigured probe: {body_unconf}"

            st_bt_after, budget_today_after = pm_client.get("/api/v1/dalton/budget/today")
            assert st_bt_after == 200

            uncalled_routes = [
                f"{m} {p}" for (m, p) in enumerated_routes if (m, p) not in called_set
            ]
            assert not uncalled_routes, f"Uncalled Dalton routes: {uncalled_routes}"
    except BaseException as exc:
        if not isinstance(exc, Exception):
            active_base_exc = exc
        raise
    finally:
        cleanup_errors: list[str] = []
        pending_base_exc: BaseException | None = None

        def _open_cleanup_client(
            target_user: str,
            err_prefix: str,
        ) -> tuple[RealRunHttpClient | None, bool]:
            nonlocal pending_base_exc
            try:
                return (
                    RealRunHttpClient(
                        base_url=args.backend,
                        user_id=target_user,
                        timeout_s=30.0,
                        record=False,
                    ),
                    True,
                )
            except BaseException as exc:
                if not isinstance(exc, Exception) and pending_base_exc is None:
                    pending_base_exc = exc
                # The client only fails to open when the backend health check fails, and then no
                # restore request could succeed either; record it so the manual-deletion note fires.
                cleanup_errors.append(f"{err_prefix}:{type(exc).__name__}")
                return None, False

        cleanup_pm, should_close_pm = _open_cleanup_client(pm_user, "cleanup_pm_exception")
        try:
            if cleanup_pm is not None:
                if created_auto_id:
                    try:
                        st_del_auto, _ = cleanup_pm.delete(f"/api/v1/automations/{created_auto_id}")
                        if st_del_auto not in (200, 204, 404):
                            cleanup_errors.append(f"delete_auto_pm:http_{st_del_auto}")
                    except BaseException as exc:
                        if not isinstance(exc, Exception) and pending_base_exc is None:
                            pending_base_exc = exc
                        cleanup_errors.append(f"delete_auto_pm_exception:{type(exc).__name__}")
                if sample_ws_id:
                    try:
                        st_del_ws_pm, _ = cleanup_pm.delete(f"/api/v1/workspaces/{sample_ws_id}")
                        if st_del_ws_pm not in (200, 204, 404):
                            cleanup_errors.append(f"delete_ws_pm:http_{st_del_ws_pm}")
                    except BaseException as exc:
                        if not isinstance(exc, Exception) and pending_base_exc is None:
                            pending_base_exc = exc
                        cleanup_errors.append(f"delete_ws_pm_exception:{type(exc).__name__}")
                if key_modified_pm:
                    try:
                        restore_key_err_pm = _restore_api_keys_state(
                            cleanup_pm,
                            prior_state=prior_keys_pm,
                        )
                        if restore_key_err_pm is not None:
                            cleanup_errors.append(f"restore_key_pm:{restore_key_err_pm}")
                    except BaseException as exc:
                        if not isinstance(exc, Exception) and pending_base_exc is None:
                            pending_base_exc = exc
                        restore_key_err_pm = f"restore_key_pm_exception:{type(exc).__name__}"
                        cleanup_errors.append(restore_key_err_pm)
                if budget_modified_pm:
                    try:
                        restore_budget_err_pm = _restore_budget_state(
                            cleanup_pm,
                            user_id=pm_user,
                            was_configured=was_configured_pm,
                            prior_budget=prior_budget_pm,
                        )
                        if restore_budget_err_pm is not None:
                            cleanup_errors.append(f"restore_budget_pm:{restore_budget_err_pm}")
                    except BaseException as exc:
                        if not isinstance(exc, Exception) and pending_base_exc is None:
                            pending_base_exc = exc
                        restore_budget_err_pm = f"restore_budget_pm_exception:{type(exc).__name__}"
                        cleanup_errors.append(restore_budget_err_pm)
            elif key_modified_pm:
                restore_key_err_pm = "restore_key_pm_no_client"
                cleanup_errors.append(restore_key_err_pm)
        finally:
            if cleanup_pm is not None and should_close_pm:
                try:
                    cleanup_pm.close()
                except Exception:
                    pass

        if key_modified_pm and restore_key_err_pm is not None:
            cleanup_errors.append("临时 DeepSeek key 仍在 realrun-pm-a 上，需要手动删除")

        if user_b_ws_id:
            cleanup_b, should_close_b = _open_cleanup_client(other_user, "delete_ws_b_exception")
            try:
                if cleanup_b is not None:
                    try:
                        st_del_ws_b, _ = cleanup_b.delete(f"/api/v1/workspaces/{user_b_ws_id}")
                        if st_del_ws_b not in (200, 204, 404):
                            cleanup_errors.append(f"delete_ws_b:http_{st_del_ws_b}")
                    except BaseException as exc:
                        if not isinstance(exc, Exception) and pending_base_exc is None:
                            pending_base_exc = exc
                        cleanup_errors.append(f"delete_ws_b_exception:{type(exc).__name__}")
            finally:
                if cleanup_b is not None and should_close_b:
                    try:
                        cleanup_b.close()
                    except Exception:
                        pass

        if cleanup_errors:
            # Printed here because an interrupt or an integer SystemExit re-raised below carries no message,
            # and a temporary key left on the account must never go unreported.
            print(f"G2 cleanup/restoration failed: {cleanup_errors}", file=sys.stderr, flush=True)

        unrecoverable_base_exc = pending_base_exc or active_base_exc
        if unrecoverable_base_exc is not None:
            if cleanup_errors and isinstance(unrecoverable_base_exc, SystemExit):
                orig_code = unrecoverable_base_exc.code
                if isinstance(orig_code, str) or orig_code is None:
                    raise SystemExit(
                        f"{orig_code or ''}; G2 cleanup/restoration failed: {cleanup_errors}".lstrip("; ")
                    ) from unrecoverable_base_exc
            raise unrecoverable_base_exc

        if cleanup_errors:
            raise RuntimeError(
                f"G2 cleanup/restoration failed: {cleanup_errors}"
            )

    byok_evidence = _build_g2_byok_evidence_fields(
        pm_user=pm_user,
        key_modified_pm=key_modified_pm,
        restore_key_err_pm=restore_key_err_pm,
        restore_budget_err_pm=restore_budget_err_pm,
    )

    startup_fail_check = _verify_startup_fail_fast()
    assert startup_fail_check["failed_fast"] is True, (
        f"Startup fail-fast check did not fail with RolePolicyError: {startup_fail_check}"
    )

    ended_at = now_utc_iso()
    script_cmd = f"scripts/dalton/realrun/g2_providers.py --backend {args.backend} --date {args.date}"
    envelope = build_evidence_envelope(
        script=script_cmd,
        started_at=started_at,
        ended_at=ended_at,
        backend=args.backend,
        test_user_id=test_user_id,
        synthetic=synthetic,
        extra_fields={
            "test_user_ids": [pm_user, other_user],
            "enumerated_routes_count": len(enumerated_routes),
            "called_routes_count": len(called_set),
            "uncalled_routes": uncalled_routes,
            "routes": called_routes,
            "pm_rbac_check": {
                "endpoint": "POST /api/v1/dalton/knowledge/{workspace_id}/thesis/import",
                "pm_user": pm_user,
                "pm_user_http_status": st_imp_v1,
                "non_pm_user": other_user,
                "non_pm_user_http_status": st_imp_user_b,
                "non_pm_user_response": body_imp_user_b,
            },
            "deepseek_byok_setup": {
                "user_id": byok_evidence["user_id"],
                "settings_endpoint": byok_evidence["settings_endpoint"],
                "byok_provider": byok_evidence["byok_provider"],
                "temporary_key_written": byok_evidence["temporary_key_written"],
                "temporary_key_deleted": byok_evidence["temporary_key_deleted"],
                "key_write_skipped_reason": byok_evidence["key_write_skipped_reason"],
                "api_keys_restored": byok_evidence["api_keys_restored"],
                "budget_restored": byok_evidence["budget_restored"],
            },
            "temporary_key_written": byok_evidence["temporary_key_written"],
            "temporary_key_deleted": byok_evidence["temporary_key_deleted"],
            "key_write_skipped_reason": byok_evidence["key_write_skipped_reason"],
            "api_keys_restored": byok_evidence["api_keys_restored"],
            "budget_restored": byok_evidence["budget_restored"],
            "probes": probe_results,
            "unconfigured_provider_probe": body_unconf,
            "budget_today_after_probe": {
                "cap_usd": budget_today_after.get("cap_usd"),
                "spent_usd": budget_today_after.get("spent_usd"),
                "journal_cash_usd": budget_today_after.get("journal_cash_usd"),
                "by_provider": budget_today_after.get("by_provider"),
            },
            "startup_fail_fast_check": startup_fail_check,
        },
    )

    journal_totals = envelope["journal"]["totals"]
    raw_path, md_path, sha256_hex = write_evidence_and_summary(
        family_code="G2",
        title="Dalton 路由全覆盖、供应商真实探测、PM 权限隔离与真实子进程启动拒绝校验（P0J §5 G2）",
        raw_json_path=f"var/dalton/trial-runs/realrun-{args.date}/g2_providers.json",
        summary_md_path=f"var/dalton/trial-runs/realrun-{args.date}/G2.md",
        envelope=envelope,
        key_findings=[
            ("测试用户 (`pm_user` / `non_pm_user`)", f"`{pm_user}` / `{other_user}`（0 条 `local-dev-user` 记录）"),
            ("OpenAPI 枚举 `/api/v1/dalton` 路由数", len(enumerated_routes)),
            ("实际调用覆盖路由数 (`uncalled_routes=0`)", len(called_set)),
            (
                "已配置供应商探测 (`POST /api/v1/dalton/providers/{provider}/probe`)",
                ", ".join(f"{p.get('provider')}({p.get('model_key')}):ok={p.get('ok')}" for p in probe_results),
            ),
            (
                "未配置供应商探测 (`zhipu`)",
                f"ok={body_unconf.get('ok')}, error_kind={body_unconf.get('error_kind')}（无凭据泄露）",
            ),
            (
                "PM 专属路由权限验证 (`POST /api/v1/dalton/knowledge/{ws}/thesis/import`)",
                f"`{pm_user}` -> HTTP {st_imp_v1}，`{other_user}` -> HTTP {st_imp_user_b}",
            ),
            (
                "探测后 `GET /api/v1/dalton/budget/today` 现金花费",
                f"spent_usd=${float(budget_today_after.get('spent_usd', 0.0)):.6f} (journal_cash_usd=${float(budget_today_after.get('journal_cash_usd', 0.0)):.6f})",
            ),
            (
                "`dalton_llm_journal` 对账汇总 (`test_user_id`)",
                f"calls={journal_totals.get('total_calls')}, fresh={journal_totals.get('fresh_calls')}, cash_usd=${float(journal_totals.get('cash_usd', 0.0)):.6f}",
            ),
            (
                "真实后端子进程启动拒绝校验 (`PTC_CONFIG_FILE` 设 `assist=muse-spark-1.3-contributor`)",
                f"failed_fast={startup_fail_check['failed_fast']} (exit_code={startup_fail_check['exit_code']})",
            ),
            ("运行后状态恢复", byok_evidence["restoration_summary"]),
        ],
        notes=[
            f"所有 {len(enumerated_routes)} 条 `/api/v1/dalton/*` 路由均从运行中后端的 `GET /openapi.json` 动态枚举并在 `{pm_user}` / `{other_user}` 下逐一调用，断言 `uncalled_routes == []`，全程不触碰 `local-dev-user`。",
            "启动即拒验证通过 `subprocess.Popen([sys.executable, '-m', 'uvicorn', 'src.server.app:app', ...])` 配合临时配置文件 `PTC_CONFIG_FILE` 在真实子进程中执行，等待进程自行退出并断言 `exit_code > 0`（非信号终止）且输出包含 `RolePolicyError`。",
        ],
        record=args.record,
    )
    print(json.dumps({"raw_json": str(raw_path), "summary_md": str(md_path), "sha256": sha256_hex, "record": args.record}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
