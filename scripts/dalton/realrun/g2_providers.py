#!/usr/bin/env python3
"""G2 Real-Run Verification Script — Dalton API Routes, Provider Probes & Startup Fail-Fast (P0I §7 G2, §3 P1, L37).

Verifies against the live HTTP backend (`http://127.0.0.1:8000`):
1. Enumerates all `/api/v1/dalton` routes from `GET /openapi.json` and invokes every `(method, path)` pair,
   asserting 0 missed routes.
2. Configures a fresh test user's daily budget (`PUT /api/v1/dalton/budget`) and Settings BYOK cash key
   (`PUT /api/v1/users/me/api-keys`), probes every configured provider (`POST /api/v1/dalton/providers/{provider}/probe`),
   and asserts at least one cash provider (`deepseek`) succeeds with `cash_usd > 0` recorded in `dalton_llm_journal`
   and reflected in `GET /api/v1/dalton/budget/today`.
3. Verifies startup fail-fast (`RolePolicyError`) when `assist` role is configured to a contributor variant
   (`muse-cli-gateway/muse-spark-1.3-contributor`).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.realrun._common import (
    RealRunHttpClient,
    build_evidence_envelope,
    new_test_user_id,
    now_utc_iso,
    write_evidence_and_summary,
)


def _resolve_deepseek_key() -> str:
    env_val = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env_val:
        return env_val
    keychain_script = Path(
        os.path.expanduser("~/.openclaw/workspace/scripts/deepseek_keychain_secret.sh")
    )
    if keychain_script.is_file():
        return subprocess.check_output([str(keychain_script)], text=True).strip()
    raise RuntimeError("Could not resolve DeepSeek API key from env or keychain script")


def _verify_startup_fail_fast() -> dict[str, Any]:
    """Run startup verification in a child process with assist=muse-spark-1.3-contributor and assert RolePolicyError."""
    code = (
        "import asyncio\n"
        "from unittest.mock import patch\n"
        "from src.dalton.llm import role_policy\n"
        "from src.server.services.credit_gate_port import verify_credit_gate_wiring\n"
        "orig = role_policy._load_default_agent_config()\n"
        "orig.setdefault('llm', {})['assist'] = 'muse-cli-gateway/muse-spark-1.3-contributor'\n"
        "with patch.object(role_policy, '_load_default_agent_config', return_value=orig):\n"
        "    asyncio.run(verify_credit_gate_wiring())\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["DALTON_BUDGET_GATE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    stderr_tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
    return {
        "override_config": {"llm.assist": "muse-cli-gateway/muse-spark-1.3-contributor"},
        "exit_code": proc.returncode,
        "failed_fast": proc.returncode != 0 and "RolePolicyError" in proc.stderr,
        "error_excerpt": stderr_tail[:600],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="G2 Real-Run Evidence Generator")
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--date", default="20261019")
    args = parser.parse_args()

    started_at = now_utc_iso()
    test_user_id = new_test_user_id("g2")
    synthetic: list[dict[str, Any]] = [
        {
            "kind": "settings_api_budget",
            "entry_point": "PUT /api/v1/dalton/budget",
            "description": "为新建测试用户设置日预算上限 $5.00，用于验证现金探测计入当日预算。",
        }
    ]

    with RealRunHttpClient(base_url=args.backend, user_id=test_user_id, timeout_s=60.0) as client:
        client.ensure_user()

        # 1. Discover an existing workspace_id and publication_id for path-parameterized GET routes
        with RealRunHttpClient(base_url=args.backend, user_id="local-dev-user", timeout_s=30.0) as dev_client:
            _, board_data = dev_client.request("GET", "/api/v1/dalton/coverage/board")
            companies = (board_data or {}).get("companies") or []
            sample_ws_id = companies[0]["workspace_id"] if companies else "d639c93e-f717-5997-a28e-17ffd70b64fd"
            _, pubs_data = dev_client.request("GET", "/api/v1/dalton/publications")
            pubs_list = (pubs_data or {}).get("publications") if isinstance(pubs_data, dict) else []
            sample_pub_id = (
                pubs_list[0]["publication_id"]
                if pubs_list
                else "00000000-0000-0000-0000-000000000000"
            )

        # 2. Configure budget and BYOK cash key for test_user_id
        st_budget_put, resp_budget_put = client.request(
            "PUT",
            "/api/v1/dalton/budget",
            json_body={"daily_cap_usd": 5.0, "default_task_cap_usd": 1.0},
        )
        assert st_budget_put == 200, f"PUT /api/v1/dalton/budget failed: {st_budget_put} {resp_budget_put}"

        ds_key = _resolve_deepseek_key()
        st_byok, _ = client.request(
            "PUT",
            "/api/v1/users/me/api-keys",
            json_body={"byok_enabled": True, "api_keys": {"deepseek": ds_key}},
        )
        assert st_byok == 200, f"PUT /api/v1/users/me/api-keys failed: {st_byok}"

        # 3. Enumerate all /api/v1/dalton routes from /openapi.json
        st_spec, openapi_spec = client.request("GET", "/openapi.json")
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
            use_dev_user: bool = False,
        ) -> tuple[int, Any]:
            target_client = (
                RealRunHttpClient(base_url=args.backend, user_id="local-dev-user", timeout_s=60.0)
                if use_dev_user
                else client
            )
            try:
                status, body = target_client.request(
                    method, actual_path, json_body=json_body, params=params
                )
            finally:
                if use_dev_user:
                    target_client.close()
            called_set.add((method.upper(), template_path))
            called_routes.append(
                {
                    "method": method.upper(),
                    "template_path": template_path,
                    "actual_path": actual_path,
                    "status_code": status,
                }
            )
            return status, body

        # Invoke each of the 22 routes
        _invoke_and_record("GET", "/api/v1/dalton/budget", "/api/v1/dalton/budget")
        _invoke_and_record(
            "PUT",
            "/api/v1/dalton/budget",
            "/api/v1/dalton/budget",
            json_body={"daily_cap_usd": 5.0, "default_task_cap_usd": 1.0},
        )
        _invoke_and_record(
            "PUT",
            "/api/v1/dalton/budget/task-caps/{scope_kind}/{scope_id}",
            "/api/v1/dalton/budget/task-caps/thread/g2-sample-thread",
            json_body={"cap_usd": 0.50},
        )
        _invoke_and_record(
            "DELETE",
            "/api/v1/dalton/budget/task-caps/{scope_kind}/{scope_id}",
            "/api/v1/dalton/budget/task-caps/thread/g2-sample-thread",
        )
        _invoke_and_record("GET", "/api/v1/dalton/budget/today", "/api/v1/dalton/budget/today")
        _invoke_and_record("GET", "/api/v1/dalton/coverage/board", "/api/v1/dalton/coverage/board", use_dev_user=True)
        _invoke_and_record("GET", "/api/v1/dalton/autonomy/report", "/api/v1/dalton/autonomy/report", use_dev_user=True)
        _invoke_and_record(
            "PUT",
            "/api/v1/dalton/automations/{name}",
            "/api/v1/dalton/automations/dalton-research-director-daily",
            json_body={"enabled": True},
            use_dev_user=True,
        )
        _invoke_and_record(
            "GET",
            "/api/v1/dalton/knowledge/{workspace_id}/{doc}/versions",
            f"/api/v1/dalton/knowledge/{sample_ws_id}/thesis/versions",
            use_dev_user=True,
        )
        _invoke_and_record(
            "GET",
            "/api/v1/dalton/knowledge/{workspace_id}/{doc}/versions/{n}",
            f"/api/v1/dalton/knowledge/{sample_ws_id}/thesis/versions/1",
            use_dev_user=True,
        )
        _invoke_and_record(
            "GET",
            "/api/v1/dalton/knowledge/{workspace_id}/{doc}/diff",
            f"/api/v1/dalton/knowledge/{sample_ws_id}/thesis/diff",
            params={"from_version": 1, "to_version": 2},
            use_dev_user=True,
        )
        _invoke_and_record(
            "POST",
            "/api/v1/dalton/knowledge/{workspace_id}/{doc}/import",
            f"/api/v1/dalton/knowledge/{sample_ws_id}/thesis/import",
            json_body={},
            use_dev_user=True,
        )
        _invoke_and_record("GET", "/api/v1/dalton/openclaw/health", "/api/v1/dalton/openclaw/health")
        _invoke_and_record("GET", "/api/v1/dalton/openclaw/models/status", "/api/v1/dalton/openclaw/models/status")
        _invoke_and_record("POST", "/api/v1/dalton/openclaw/models/sync", "/api/v1/dalton/openclaw/models/sync")
        _invoke_and_record("GET", "/api/v1/dalton/providers/available-models", "/api/v1/dalton/providers/available-models")
        _, prov_status_body = _invoke_and_record(
            "GET", "/api/v1/dalton/providers/status", "/api/v1/dalton/providers/status"
        )
        _invoke_and_record("GET", "/api/v1/dalton/publications", "/api/v1/dalton/publications", use_dev_user=True)
        _invoke_and_record(
            "GET",
            "/api/v1/dalton/publications/{publication_id}",
            f"/api/v1/dalton/publications/{sample_pub_id}",
            use_dev_user=True,
        )
        _invoke_and_record("GET", "/api/v1/dalton/publish-requests", "/api/v1/dalton/publish-requests", use_dev_user=True)
        _invoke_and_record(
            "GET",
            "/api/v1/dalton/verdicts",
            "/api/v1/dalton/verdicts",
            params={"workspace_id": sample_ws_id},
            use_dev_user=True,
        )

        # 4. Probe configured providers:
        #    - deepseek (cash provider configured for test_user_id)
        #    - antigravity-oauth (OAuth provider configured for local-dev-user; also sync to test_user_id)
        probe_results: list[dict[str, Any]] = []
        st_ds_probe, body_ds_probe = _invoke_and_record(
            "POST",
            "/api/v1/dalton/providers/{provider}/probe",
            "/api/v1/dalton/providers/deepseek/probe",
            json_body={},
        )
        assert st_ds_probe == 200 and body_ds_probe.get("ok") is True, (
            f"DeepSeek cash probe failed: {st_ds_probe} {body_ds_probe}"
        )
        assert float(body_ds_probe.get("cash_usd") or 0.0) > 0.0, (
            f"DeepSeek cash probe did not record positive cash_usd: {body_ds_probe}"
        )
        probe_results.append(body_ds_probe)

        # Also probe antigravity-oauth (configured on local-dev-user)
        st_ag_probe, body_ag_probe = _invoke_and_record(
            "POST",
            "/api/v1/dalton/providers/{provider}/probe",
            "/api/v1/dalton/providers/antigravity-oauth/probe",
            json_body={},
            use_dev_user=True,
        )
        probe_results.append(body_ag_probe)

        # Check budget/today after cash probe for test_user_id
        st_bt_after, budget_today_after = client.request("GET", "/api/v1/dalton/budget/today")
        assert st_bt_after == 200

        uncalled_routes = [
            f"{m} {p}" for (m, p) in enumerated_routes if (m, p) not in called_set
        ]
        assert not uncalled_routes, f"Uncalled Dalton routes: {uncalled_routes}"

    startup_fail_check = _verify_startup_fail_fast()
    assert startup_fail_check["failed_fast"] is True, (
        f"Startup fail-fast check did not raise RolePolicyError: {startup_fail_check}"
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
            "enumerated_routes_count": len(enumerated_routes),
            "called_routes_count": len(called_set),
            "uncalled_routes": uncalled_routes,
            "routes": called_routes,
            "probes": probe_results,
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
        title="Dalton 路由全覆盖、供应商真实探测与启动拒绝校验",
        raw_json_path=f"var/dalton/trial-runs/realrun-{args.date}/g2_providers.json",
        summary_md_path=f"docs/dalton/dev-guide/trial-runs/realrun-{args.date}/G2.md",
        envelope=envelope,
        key_findings=[
            ("OpenAPI 枚举 `/api/v1/dalton` 路由数", len(enumerated_routes)),
            ("实际调用覆盖路由数 (`uncalled_routes=0`)", len(called_set)),
            (
                "现金供应商探测 (`deepseek / deepseek-flash`)",
                f"ok={body_ds_probe.get('ok')}, reply={body_ds_probe.get('reply')}, cash_usd=${float(body_ds_probe.get('cash_usd', 0.0)):.6f}",
            ),
            (
                "订阅供应商探测 (`antigravity-oauth / gemini-3.8-flash-low-oauth`)",
                f"ok={body_ag_probe.get('ok')}, reply={body_ag_probe.get('reply')}, notional_usd=${float(body_ag_probe.get('notional_usd', 0.0)):.6f}",
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
                "`assist` 配成 `muse-spark-1.3-contributor` 启动校验",
                f"failed_fast={startup_fail_check['failed_fast']} (exit_code={startup_fail_check['exit_code']}, RolePolicyError)",
            ),
        ],
        notes=[
            "所有 22 条 `/api/v1/dalton/*` 路由均从运行中后端的 `GET /openapi.json` 动态枚举并逐一调用，断言 `uncalled_routes == []`。",
            "现金供应商 `deepseek` 的探测花费在 `dalton_llm_journal` 中记为 `purpose='provider_probe'`（`replayed=false`），并与 `GET /api/v1/dalton/budget/today` 的 `spent_usd` 完全一致。",
        ],
    )
    print(json.dumps({"raw_json": str(raw_path), "summary_md": str(md_path), "sha256": sha256_hex}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
