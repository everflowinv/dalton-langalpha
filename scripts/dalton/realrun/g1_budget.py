#!/usr/bin/env python3
"""G1 Real-Run Verification Script — Daily Cash Budget Cap, Cash Subagent Spend, BYOK Cash Key & Review Cache (P0I §7 G1, L37).

Verifies against the live HTTP backend (`http://127.0.0.1:8000`) for a fresh test user:
1. Scenario ② (Settings BYOK cash key spend counted):
   - Configures `PUT /api/v1/users/me/api-keys` with the user's `deepseek` cash API key and `PUT /api/v1/dalton/budget`
     with `daily_cap_usd = --cap-usd` (default `$0.30`; must stay above `budget.admission_floor_usd` — default $0.25 —
     so the run starts through the normal admission check, and below P0I's $0.50 total-spend cap).
   - Triggers `POST /api/v1/dalton/providers/deepseek/probe` and verifies `GET /api/v1/dalton/budget/today` reflects
     `spent_usd > 0`, `journal_cash_usd > 0`, and `by_provider.deepseek.cash_usd > 0`.
2. Scenario ③ (Second review call hits cache):
   - In a live conversation thread (`POST /api/v1/threads/messages`, lead model `claude-sonnet-5-5-low-oauth`),
     creates a brief draft `drafts/g1_review_cache.md` and calls `publish_report` twice on the same draft.
   - Verifies that the first `publish.review` call executes fresh (`replayed=false`, `total_tokens > 0`) and the second
     `publish.review` call hits the `dalton_llm_journal` cache (`replayed=true`, `cached=true`, `cash_usd=0.0`,
     `usage.replay_count >= 1`).
3. Scenario ① (Claude subscription lead model dispatches cash subagent `deepseek-flash` until admission would refuse, next turn rejected with HTTP 429):
   - Overrides `general-purpose` subagent to `deepseek-flash` via `PUT /api/v1/subagents/general-purpose`.
   - Sends conversation turn(s) via `POST /api/v1/threads/{thread_id}/messages` (`llm_model="claude-sonnet-5-5-low-oauth"`)
     where the lead model dispatches the `general-purpose` (`deepseek-flash`) subagent until admission would refuse
     the next turn, i.e. `cap_usd - spent_usd < budget.admission_floor_usd` (the gate's own condition, `gate.py`).
   - Sends the subsequent turn via `POST /api/v1/threads/{thread_id}/messages` and asserts HTTP `429` with
     `detail.type == "dalton_daily_budget"`.
   - Captures a screenshot of the frontend rejection banner (`var/dalton/trial-runs/realrun-<date>/g1_budget_exhausted_banner.png`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.realrun._common import (  # noqa: E402
    RealRunHttpClient,
    build_evidence_envelope,
    build_realrun_workspace_config,
    default_shanghai_date,
    load_repo_dotenv,
    now_utc_iso,
    preflight_realrun_script,
    require_internal_service_token,
    write_evidence_and_summary,
)
from scripts.dalton.realrun.check_env import (  # noqa: E402
    _restore_api_keys_state,
    _restore_budget_state,
    _restore_subagent_state,
    _snapshot_api_keys_state,
    _snapshot_subagent_state,
)
from scripts.dalton.realrun.journal_dump import build_dsn_from_env  # noqa: E402


def _resolve_deepseek_key() -> str:
    env_val = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env_val:
        return env_val
    keychain_script = Path(
        os.path.expanduser("~/.openclaw/workspace/scripts/deepseek_keychain_secret.sh")
    )
    if keychain_script.is_file():
        return subprocess.check_output([str(keychain_script), "get"], text=True).strip()
    raise RuntimeError("Could not resolve DeepSeek API key from DEEPSEEK_API_KEY or keychain script")


def _extract_sse_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    thread_id = None
    run_id = None
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    text_chunks: list[str] = []
    run_end: dict[str, Any] | None = None
    errors: list[Any] = []

    for ev in events:
        et = ev.get("event")
        data = ev.get("data")
        if et == "metadata" and isinstance(data, dict):
            thread_id = data.get("thread_id") or thread_id
            run_id = data.get("run_id") or run_id
        elif et == "tool_calls" and isinstance(data, dict):
            tool_calls.append(
                {
                    "agent": data.get("agent"),
                    "tool_calls": [
                        {"name": tc.get("name"), "id": tc.get("id")}
                        for tc in (data.get("tool_calls") or [])
                        if isinstance(tc, dict)
                    ],
                }
            )
        elif et == "tool_call_result" and isinstance(data, dict):
            tool_results.append(
                {
                    "tool_call_id": data.get("tool_call_id"),
                    "content_preview": str(data.get("content") or "")[:160],
                }
            )
        elif et == "message_chunk" and isinstance(data, dict):
            if data.get("content_type") == "text" and data.get("content"):
                text_chunks.append(str(data.get("content")))
        elif et == "run_end" and isinstance(data, dict):
            run_end = data
            thread_id = data.get("thread_id") or thread_id
            run_id = data.get("run_id") or run_id
        elif et == "error":
            errors.append(data)

    return {
        "thread_id": thread_id,
        "run_id": run_id,
        "tool_calls_count": len(tool_calls),
        "tool_calls": tool_calls,
        "tool_results_count": len(tool_results),
        "tool_results": tool_results,
        "assistant_reply_preview": "".join(text_chunks)[:300],
        "run_end": run_end,
        "errors": errors,
    }


def _fetch_publish_requests_for_thread(thread_id: str) -> list[dict[str, Any]]:
    dsn = build_dsn_from_env(None)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT publish_request_id::text AS publish_request_id,
                       slug,
                       status,
                       review,
                       created_at
                FROM dalton_publish_requests
                WHERE conversation_thread_id = %s
                ORDER BY created_at ASC
                """,
                (thread_id,),
            )
            rows = cur.fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                out.append(
                    {
                        "publish_request_id": r["publish_request_id"],
                        "slug": r["slug"],
                        "status": r["status"],
                        "review": r["review"],
                        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                    }
                )
            return out


def _is_allowed_auth_header_origin(
    request_url: str,
    *,
    frontend_url: str,
    backend_url: str = "http://127.0.0.1:8000",
) -> bool:
    """Return True only when `request_url` targets the configured frontend or backend origin
    (including localhost <-> 127.0.0.1 equivalence on the same port). External requests
    (such as `https://fonts.googleapis.com`) must never receive `X-Service-Token` (Hard Rule 9).
    """
    from urllib.parse import urlsplit

    req = urlsplit(request_url)
    if req.scheme not in ("http", "https") or not req.hostname:
        return False

    allowed_origins: set[tuple[str, str, int | None]] = set()
    for base in (frontend_url, backend_url):
        parsed = urlsplit(base)
        if not parsed.scheme or not parsed.hostname:
            continue
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        allowed_origins.add((parsed.scheme, parsed.hostname.lower(), port))
        if parsed.hostname.lower() in ("localhost", "127.0.0.1"):
            allowed_origins.add((parsed.scheme, "localhost", port))
            allowed_origins.add((parsed.scheme, "127.0.0.1", port))

    req_port = req.port or (443 if req.scheme == "https" else 80)
    return (req.scheme, req.hostname.lower(), req_port) in allowed_origins


def _handle_scoped_auth_route(
    route: Any,
    request: Any,
    *,
    frontend_url: str,
    backend_url: str = "http://127.0.0.1:8000",
    user_id: str,
    svc_token: str,
) -> None:
    """Playwright route handler that injects `X-User-Id` and `X-Service-Token` only on
    requests targeting `frontend_url` or `backend_url`, forwarding all other requests untouched.
    """
    if _is_allowed_auth_header_origin(
        request.url, frontend_url=frontend_url, backend_url=backend_url
    ):
        headers = dict(request.headers)
        headers["X-User-Id"] = user_id
        headers["X-Service-Token"] = svc_token
        route.continue_(headers=headers)
    else:
        route.continue_()


def _capture_rejection_banner_screenshot(
    *,
    output_path: Path,
    frontend_url: str,
    backend_url: str = "http://127.0.0.1:8000",
    user_id: str,
    workspace_id: str,
    thread_id: str,
) -> tuple[dict[str, Any], bytes]:
    """Open the live frontend application (`frontend_url`) in headless Chromium as `user_id`,
    trigger the budget-exhausted chat submission on the live page so the real UI renders the
    rejection alert banner, and return `(metadata, png_bytes)` without writing to disk before the gate.
    """
    from playwright.sync_api import sync_playwright

    svc_token = require_internal_service_token()
    target_url = f"{frontend_url.rstrip('/')}/chat/t/{thread_id}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            page.route(
                "**/*",
                lambda route, request: _handle_scoped_auth_route(
                    route,
                    request,
                    frontend_url=frontend_url,
                    backend_url=backend_url,
                    user_id=user_id,
                    svc_token=svc_token,
                ),
            )
            # Not "networkidle": the chat page holds the per-user thread-events SSE feed open, so the
            # network never goes idle. The explicit textarea and banner waits below gate the capture.
            resp = page.goto(target_url, wait_until="load", timeout=30000)
            assert resp is not None and resp.status < 400, (
                f"Failed to load frontend chat route {target_url}: status={getattr(resp, 'status', None)}"
            )

            textarea = page.locator("textarea").first
            textarea.wait_for(state="visible", timeout=15000)
            # A test user that never dismissed the page intros gets an onboarding dialog over the chat;
            # it swallows Enter, so the 429 turn would never be sent. Close it the way a user would.
            dialog = page.get_by_role("dialog")
            for _ in range(5):
                if not (dialog.count() and dialog.first.is_visible()):
                    break
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
            assert not (dialog.count() and dialog.first.is_visible()), "Onboarding dialog still covers the chat page"
            textarea.fill("继续下一轮分析（日现金额度耗尽验证）")
            textarea.press("Enter")

            banner = page.get_by_text("今日预算").first
            banner.wait_for(state="visible", timeout=15000)
            assert banner.is_visible(), (
                "Expected budget-exhausted banner ('今日预算') to be visible before taking screenshot"
            )

            raw_bytes = page.screenshot(full_page=True)
        finally:
            browser.close()

    return (
        {
            "screenshot_path": str(output_path.relative_to(REPO_ROOT)),
            "frontend_url": target_url,
            "bytes": len(raw_bytes),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        },
        raw_bytes,
    )


def _admission_floor_usd() -> Decimal:
    """`budget.admission_floor_usd` from the committed `src/dalton/dalton.yaml`.

    The gate refuses a turn once `cap - spent < floor`, so spend can never reach the cap itself. Under
    `--record` the version gate guarantees the backend runs this same commit, hence this same file.
    """
    raw = yaml.safe_load((REPO_ROOT / "src" / "dalton" / "dalton.yaml").read_text(encoding="utf-8")) or {}
    return Decimal(str((raw.get("budget") or {}).get("admission_floor_usd") or 0.25))


def _gate_would_refuse(today: dict[str, Any], floor_usd: Decimal) -> bool:
    """Mirror `enforce_local_admission` (`src/dalton/budget/gate.py`) on a `/budget/today` payload.

    Decimal, like the gate: float subtraction lands on the wrong side of the floor at the boundary. The
    payload's amounts are floats, so this is a prediction for when to stop spending; the real 429 on the
    next turn stays the check of record.
    """
    available = Decimal(str(today.get("cap_usd") or 0)) - Decimal(str(today.get("spent_usd") or 0))
    return not today.get("configured", True) or available <= 0 or available < floor_usd


def _cleanup_g1_state(
    *,
    backend_url: str,
    user_a: str,
    workspace_id: str | None,
    subagent_modified_a: bool,
    prior_subagent_a: dict[str, Any] | None,
    key_modified_a: bool,
    prior_keys_a: dict[str, Any] | None,
    budget_modified_a: bool,
    was_configured_a: bool,
    prior_budget_a: dict[str, Any] | None,
    user_b: str,
    ws_b_id: str | None,
    key_modified_b: bool,
    prior_keys_b: dict[str, Any] | None,
    budget_modified_b: bool,
    was_configured_b: bool,
    prior_budget_b: dict[str, Any] | None,
) -> dict[str, str | None]:
    """Run all G1 cleanup steps in independent `try` blocks and raise if any step failed."""
    errors: dict[str, str | None] = {
        "ws_a_err": None,
        "restore_subagent_err_a": None,
        "restore_key_err_a": None,
        "restore_err_a": None,
        "ws_b_err": None,
        "delete_key_err_b": None,
        "restore_err_b": None,
    }

    if workspace_id or subagent_modified_a or key_modified_a or budget_modified_a:
        try:
            with RealRunHttpClient(base_url=backend_url, user_id=user_a, timeout_s=30.0) as cleanup_a:
                if workspace_id:
                    try:
                        st_del_a, _ = cleanup_a.delete(f"/api/v1/workspaces/{workspace_id}")
                        if st_del_a not in (200, 204, 404):
                            errors["ws_a_err"] = f"workspace_a_delete_http_{st_del_a}"
                    except Exception as exc:
                        errors["ws_a_err"] = f"workspace_a_delete_exception:{type(exc).__name__}"

                if subagent_modified_a:
                    try:
                        errors["restore_subagent_err_a"] = _restore_subagent_state(
                            cleanup_a,
                            prior_state=prior_subagent_a,
                        )
                    except Exception as exc:
                        errors["restore_subagent_err_a"] = f"subagent_a_restore_exception:{type(exc).__name__}"

                if key_modified_a:
                    try:
                        errors["restore_key_err_a"] = _restore_api_keys_state(
                            cleanup_a,
                            prior_state=prior_keys_a,
                        )
                    except Exception as exc:
                        errors["restore_key_err_a"] = f"key_a_restore_exception:{type(exc).__name__}"

                if budget_modified_a:
                    try:
                        errors["restore_err_a"] = _restore_budget_state(
                            cleanup_a,
                            user_id=user_a,
                            was_configured=was_configured_a,
                            prior_budget=prior_budget_a,
                        )
                    except Exception as exc:
                        errors["restore_err_a"] = f"budget_a_restore_exception:{type(exc).__name__}"
        except Exception as exc:
            errors["restore_err_a"] = f"cleanup_a_exception:{type(exc).__name__}"

    if ws_b_id or key_modified_b or budget_modified_b:
        try:
            with RealRunHttpClient(base_url=backend_url, user_id=user_b, timeout_s=30.0) as cleanup_b:
                if ws_b_id:
                    try:
                        st_del_b, _ = cleanup_b.delete(f"/api/v1/workspaces/{ws_b_id}")
                        if st_del_b not in (200, 204, 404):
                            errors["ws_b_err"] = f"workspace_b_delete_http_{st_del_b}"
                    except Exception as exc:
                        errors["ws_b_err"] = f"workspace_b_delete_exception:{type(exc).__name__}"

                if key_modified_b:
                    try:
                        errors["delete_key_err_b"] = _restore_api_keys_state(
                            cleanup_b,
                            prior_state=prior_keys_b,
                        )
                    except Exception as exc:
                        errors["delete_key_err_b"] = f"key_b_restore_exception:{type(exc).__name__}"

                if budget_modified_b:
                    try:
                        errors["restore_err_b"] = _restore_budget_state(
                            cleanup_b,
                            user_id=user_b,
                            was_configured=was_configured_b,
                            prior_budget=prior_budget_b,
                        )
                    except Exception as exc:
                        errors["restore_err_b"] = f"budget_b_restore_exception:{type(exc).__name__}"
        except Exception as exc:
            errors["restore_err_b"] = f"cleanup_b_exception:{type(exc).__name__}"

    if any(v is not None for v in errors.values()):
        raise RuntimeError(
            f"G1 post-run restoration failed (P0J §8.1 #19): "
            f"ws_a={errors['ws_a_err']}, "
            f"user_a_budget={errors['restore_err_a']}, "
            f"user_a_key={errors['restore_key_err_a']}, "
            f"user_a_subagent={errors['restore_subagent_err_a']}, "
            f"ws_b={errors['ws_b_err']}, "
            f"user_b_budget={errors['restore_err_b']}, "
            f"user_b_key={errors['delete_key_err_b']}"
        )

    return errors


def _ensure_deepseek_byok_for_g1(
    client: RealRunHttpClient,
    prior_keys_state: dict[str, Any] | None,
) -> bool:
    """Write a temporary DeepSeek BYOK key only when the user has none configured (D6).

    Returns True when a temporary key was written (`PUT /api/v1/users/me/api-keys`),
    or False when an existing user key was preserved without modification.
    """
    if bool((prior_keys_state or {}).get("had_user_deepseek_key")):
        return False
    ds_key = _resolve_deepseek_key()
    st_k, _ = client.put(
        "/api/v1/users/me/api-keys",
        json_body={"byok_enabled": True, "api_keys": {"deepseek": ds_key}},
    )
    assert st_k == 200, f"PUT /api/v1/users/me/api-keys failed: status={st_k}"
    return True


def _assert_g1_added_cash_spend(
    *,
    baseline_spent_a: float,
    final_spent_a: float,
    baseline_spent_b: float,
    final_spent_b: float,
) -> tuple[float, float, float]:
    """Verify G1 newly added cash spend bounds (D5 / P0J §8.1 #19)."""
    added_spent_a = round(float(final_spent_a) - float(baseline_spent_a), 6)
    added_spent_b = round(float(final_spent_b) - float(baseline_spent_b), 6)
    total_added_spent_usd = round(added_spent_a + added_spent_b, 6)
    assert added_spent_b <= 0.05, (
        f"Expected user_b newly added cash spend <= 0.05 USD, got ${added_spent_b:.6f}"
    )
    assert total_added_spent_usd <= 0.50, (
        f"Expected user_a + user_b newly added cash spend <= 0.50 USD, got ${total_added_spent_usd:.6f} "
        f"(user_a=${added_spent_a:.6f}, user_b=${added_spent_b:.6f})"
    )
    return added_spent_a, added_spent_b, total_added_spent_usd


def _build_g1_byok_evidence_fields(
    *,
    key_modified_a: bool,
    key_modified_b: bool,
    cleanup_result: dict[str, str | None],
) -> dict[str, Any]:
    """Build truthful evidence fields for G1 BYOK key setup/cleanup under D6."""
    temp_deleted_a = bool(key_modified_a and cleanup_result.get("restore_key_err_a") is None)
    temp_deleted_b = bool(key_modified_b and cleanup_result.get("delete_key_err_b") is None)
    user_a_key_desc = (
        f"临时 BYOK Key 已删除={temp_deleted_a}"
        if key_modified_a
        else "既有 BYOK Key 保留未改动（未写入、未删除）"
    )
    user_b_key_desc = (
        f"临时 BYOK Key 已删除={temp_deleted_b}"
        if key_modified_b
        else "既有 BYOK Key 保留未改动（未写入、未删除）"
    )
    return {
        "scenario_1_api_keys_restored": (
            (cleanup_result.get("restore_key_err_a") is None)
            if key_modified_a
            else "not_applicable"
        ),
        "scenario_2_settings_endpoint": (
            "PUT /api/v1/users/me/api-keys"
            if key_modified_a
            else "GET /api/v1/users/me/api-keys (preexisting key preserved)"
        ),
        "scenario_2_temporary_key_written": bool(key_modified_a),
        "scenario_2_temporary_key_deleted": temp_deleted_a,
        "scenario_2_key_write_skipped_reason": (
            None if key_modified_a else "preexisting_user_deepseek_key_preserved"
        ),
        "user_b_temporary_key_written": bool(key_modified_b),
        "user_b_key_deleted": temp_deleted_b,
        "user_b_key_write_skipped_reason": (
            None if key_modified_b else "preexisting_user_deepseek_key_preserved"
        ),
        "byok_a_action_label": (
            "PUT /api/v1/users/me/api-keys（写入临时 Key）"
            if key_modified_a
            else "GET /api/v1/users/me/api-keys（复用既有 Key，D6 未覆盖写入）"
        ),
        "restoration_summary": (
            f"用户 A 预算已恢复={cleanup_result.get('restore_err_a') is None}（{user_a_key_desc}），"
            f"用户 B 预算已恢复={cleanup_result.get('restore_err_b') is None}（{user_b_key_desc}）"
        ),
        "user_b_cleanup_note": (
            (
                "结束时已删除用户 B 本轮写入的临时 key 并恢复双方原预算配置。"
                if temp_deleted_b
                else f"结束时删除用户 B 本轮写入的临时 key 失败（{cleanup_result.get('delete_key_err_b')}），预算恢复状态见 cleanup_result。"
            )
            if key_modified_b
            else "因用户 B 原已有 DeepSeek key（D6），本轮未覆盖写入、结束时亦保留未删除，并恢复双方原预算配置。"
        ),
    }


def main() -> int:
    load_repo_dotenv()
    parser = argparse.ArgumentParser(description="G1 Real-Run Evidence Generator (P0I §7 G1)")
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--frontend", default="http://localhost:5173")
    parser.add_argument("--cap-usd", type=float, default=0.30)
    parser.add_argument("--date", default=default_shanghai_date())
    parser.add_argument("--record", action="store_true", help="Write evidence JSON and summary under var/dalton/")
    args = parser.parse_args()

    preflight_realrun_script("G1", record=args.record, backend=args.backend)

    started_at = now_utc_iso()
    user_a = "realrun-pm-a"
    user_b = "realrun-user-b"
    test_user_id = f"{user_a},{user_b}"
    cap_delta_usd = float(args.cap_usd)
    user_b_cap_delta_usd = 0.30

    prior_budget_a: dict[str, Any] | None = None
    was_configured_a = False
    budget_modified_a = False
    prior_keys_a: dict[str, Any] | None = None
    key_modified_a = False
    prior_subagent_a: dict[str, Any] | None = None
    subagent_modified_a = False
    workspace_id: str | None = None

    prior_budget_b: dict[str, Any] | None = None
    was_configured_b = False
    budget_modified_b = False
    prior_keys_b: dict[str, Any] | None = None
    key_modified_b = False
    ws_b_id: str | None = None

    cleanup_result: dict[str, str | None] = {}

    try:
        with RealRunHttpClient(base_url=args.backend, user_id=user_a, timeout_s=300.0) as client:
            client.ensure_user()

            st_prior_a, body_prior_a = client.get("/api/v1/dalton/budget")
            if st_prior_a == 200 and isinstance(body_prior_a, dict):
                prior_budget_a = body_prior_a
                was_configured_a = bool(body_prior_a.get("configured"))

            prior_keys_a = _snapshot_api_keys_state(client)
            prior_subagent_a = _snapshot_subagent_state(client, "general-purpose")

            st_init_today_a, init_today_a = client.get("/api/v1/dalton/budget/today")
            assert st_init_today_a == 200 and isinstance(init_today_a, dict), (
                f"GET /api/v1/dalton/budget/today failed for {user_a}: {st_init_today_a} {init_today_a}"
            )
            baseline_spent_a = float(init_today_a.get("spent_usd") or 0.0)
            cap_usd = round(baseline_spent_a + cap_delta_usd, 6)
            floor_usd = _admission_floor_usd()
            assert Decimal(str(cap_delta_usd)) > floor_usd, (
                f"--cap-usd {cap_delta_usd} must exceed admission_floor_usd {floor_usd}, or the first turn is refused"
            )

            # 1. Set daily budget cap to (today's already spent cash + $0.30) via Settings API for user A (P0J §8.1 #19)
            st_b, budget_cfg = client.put(
                "/api/v1/dalton/budget",
                json_body={"daily_cap_usd": cap_usd, "default_task_cap_usd": 0.50},
            )
            assert st_b == 200, f"PUT /api/v1/dalton/budget failed: {st_b} {budget_cfg}"
            budget_modified_a = True

            # 2. Scenario ②: Configure BYOK cash key in Settings (`PUT /api/v1/users/me/api-keys`)
            #    only when the user does NOT already have a DeepSeek key (D6), and verify cash spend
            #    is counted in `GET /api/v1/dalton/budget/today`
            key_modified_a = _ensure_deepseek_byok_for_g1(client, prior_keys_a)

            _, today_before_probe = client.get("/api/v1/dalton/budget/today")
            spent_before_probe = float(today_before_probe.get("spent_usd") or 0.0)
            st_probe, probe_resp = client.post(
                "/api/v1/dalton/providers/deepseek/probe",
                json_body={"model_key": "deepseek-flash"},
            )
            assert st_probe == 200 and probe_resp.get("ok") is True, (
                f"DeepSeek BYOK cash probe failed: {st_probe} {probe_resp}"
            )
            _, today_after_probe = client.get("/api/v1/dalton/budget/today")
            assert float(today_after_probe.get("spent_usd") or 0.0) > spent_before_probe, (
                f"Expected spent_usd to increase after BYOK cash probe (before={spent_before_probe}), got {today_after_probe}"
            )

            # 3. Configure `general-purpose` subagent to use cash model `deepseek-flash`
            sa_version = str((prior_subagent_a or {}).get("version") or "")
            st_sa_put, sa_put_resp = client.put(
                "/api/v1/subagents/general-purpose",
                json_body={"version": sa_version, "fields": {"model": "deepseek-flash"}},
            )
            assert st_sa_put == 200, f"PUT /api/v1/subagents/general-purpose failed: {st_sa_put} {sa_put_resp}"
            subagent_modified_a = True

            # Create an analyst company workspace so publish_report and Task tools are bound
            st_ws, ws_body = client.post(
                "/api/v1/workspaces",
                json_body={
                    "name": "GOOGL",
                    "description": "G1 real-run budget and review cache verification",
                    "config": build_realrun_workspace_config("g1_budget.py"),
                },
            )
            assert st_ws in (200, 201) and ws_body.get("workspace_id"), (
                f"POST /api/v1/workspaces failed: {st_ws} {ws_body}"
            )
            workspace_id = str(ws_body["workspace_id"])

            # 4. Scenario ③: Independent review second call hits cache (`replayed=true, cash_usd=0.0`)
            #    Lead model (`claude-sonnet-5-5-low-oauth`) writes a short draft with [估计] figures
            #    and calls `publish_report` twice on the same draft in the same thread.
            turn_review_prompt = (
                "请依次执行以下三步（不要调用其他无关工具）：\n"
                "1. 调用 Write 工具写入文件 `drafts/g1_review_cache.md`，内容严格为：\n"
                "   `# GOOGL 简评\\n\\n## 核心观点\\n管理层预计 2026 年资本开支约为 $750 亿 [估计]，云业务营业利润率预计提升至 18.5% [估计]。`\n"
                "2. 第一次调用 `publish_report` 工具，参数：`draft_path='drafts/g1_review_cache.md'`, "
                "`title='GOOGL 简评'`, `kind='event_note'`, `slug='g1-review-cache-1'`。\n"
                "3. 等第一次 `publish_report` 返回后，立即第二次调用 `publish_report` 工具，参数："
                "`draft_path='drafts/g1_review_cache.md'`, `title='GOOGL 简评'`, `kind='event_note'`, `slug='g1-review-cache-2'`。\n"
                "完成后用一句话汇报两次调用状态。"
            )
            st_rev_sse, rev_events, rev_err = client.stream_sse(
                "POST",
                "/api/v1/threads/messages",
                json_body={
                    "workspace_id": workspace_id,
                    "llm_model": "claude-sonnet-5-5-low-oauth",
                    "messages": [{"role": "user", "content": turn_review_prompt}],
                },
                max_events=2500,
            )
            assert st_rev_sse == 200 and rev_err is None, (
                f"Review cache turn failed: {st_rev_sse} {rev_err}"
            )
            rev_turn_summary = _extract_sse_summary(rev_events)
            thread_id = str(rev_turn_summary["thread_id"] or "")
            assert thread_id, f"Missing thread_id from review turn: {rev_turn_summary}"

            pub_requests = _fetch_publish_requests_for_thread(thread_id)
            if len(pub_requests) < 2:
                st_rev2, rev2_events, rev2_err = client.stream_sse(
                    "POST",
                    f"/api/v1/threads/{thread_id}/messages",
                    json_body={
                        "llm_model": "claude-sonnet-5-5-low-oauth",
                        "messages": [
                            {
                                "role": "user",
                                "content": (
                                    "请立即再次调用 `publish_report` 工具（不要修改文件 `drafts/g1_review_cache.md`），"
                                    "参数：`draft_path='drafts/g1_review_cache.md'`, `title='GOOGL 简评'`, "
                                    "`kind='event_note'`, `slug='g1-review-cache-2'`。"
                                ),
                            }
                        ],
                    },
                    max_events=2000,
                )
                assert st_rev2 == 200 and rev2_err is None, f"Second publish_report turn failed: {st_rev2} {rev2_err}"
                pub_requests = _fetch_publish_requests_for_thread(thread_id)

            assert len(pub_requests) >= 2, f"Expected >= 2 publish_requests, got {pub_requests}"
            first_rev = (pub_requests[0].get("review") or {}).get("usage") or {}
            second_rev = (pub_requests[1].get("review") or {}).get("usage") or {}
            assert first_rev.get("replayed") is False, f"Expected first review replayed=False, got {first_rev}"
            assert second_rev.get("replayed") is True, f"Expected second review replayed=True, got {second_rev}"
            assert float(second_rev.get("cash_usd") or 0.0) == 0.0, (
                f"Expected second review cash_usd=0.0, got {second_rev}"
            )

            # 5. Scenario ①: Lead model (`claude-sonnet-5-5-low-oauth`) dispatches cash subagent (`deepseek-flash`)
            #    until admission would refuse the next turn (`cap_usd - spent_usd < admission_floor_usd`, the gate's own
            #    condition), then the next turn via `POST /api/v1/threads/{thread_id}/messages` is rejected with HTTP 429
            #    (`dalton_daily_budget`).
            subagent_turns: list[dict[str, Any]] = []
            spend_deadline = time.monotonic() + 600.0
            turn_idx = 0
            reached_cap = False
            while time.monotonic() < spend_deadline:
                _, cur_today = client.get("/api/v1/dalton/budget/today")
                if _gate_would_refuse(cur_today, floor_usd) and float(cur_today.get("open_reserved_usd") or 0.0) == 0.0:
                    reached_cap = True
                    break

                turn_idx += 1
                prev_settled_only = float(cur_today.get("settled_usd") or 0.0)
                subagent_prompt = (
                    f"请在本轮响应中同时并行调用 3 次 `Task` 工具（3 个并行子任务均使用 `subagent_type='general-purpose'`，分别命名为 `批次{turn_idx}-A`、`批次{turn_idx}-B`、`批次{turn_idx}-C`）。\n"
                    "在每个 `Task` 的 `prompt` 中，要求该子智能体严格按顺序分 3 轮调用 `Bash` 工具（每轮等待上一轮返回后再发起下一轮，每次输出 170 行随机数据以保留在上下文内）：\n"
                    "第 1 轮在子智能体中执行：`python3 -c \"import uuid; print('\\n'.join(f'R1_{i:03d}_{uuid.uuid4().hex}_{uuid.uuid4().hex}' for i in range(170)))\"`；\n"
                    "第 2 轮在子智能体中执行：`python3 -c \"import uuid; print('\\n'.join(f'R2_{i:03d}_{uuid.uuid4().hex}_{uuid.uuid4().hex}' for i in range(170)))\"`；\n"
                    "第 3 轮在子智能体中执行：`python3 -c \"import uuid; print('\\n'.join(f'R3_{i:03d}_{uuid.uuid4().hex}_{uuid.uuid4().hex}' for i in range(170)))\"`；\n"
                    "第 3 轮返回后，子智能体撰写 400 字详细财务敏感性结论并返回。"
                )
                st_sub, sub_events, sub_err = client.stream_sse(
                    "POST",
                    f"/api/v1/threads/{thread_id}/messages",
                    json_body={
                        "llm_model": "claude-sonnet-5-5-low-oauth",
                        "messages": [{"role": "user", "content": subagent_prompt}],
                    },
                    max_events=2500,
                )
                assert st_sub == 200 and sub_err is None, (
                    f"Subagent turn {turn_idx} failed: {st_sub} {sub_err}"
                )
                sub_summary = _extract_sse_summary(sub_events)

                after_turn_today = cur_today
                settle_deadline = time.monotonic() + 120.0
                settled_confirmed = False
                while time.monotonic() < settle_deadline:
                    time.sleep(2.0)
                    _, after_turn_today = client.get("/api/v1/dalton/budget/today")
                    new_settled_only = float(after_turn_today.get("settled_usd") or 0.0)
                    open_res = float(after_turn_today.get("open_reserved_usd") or 0.0)
                    if new_settled_only > prev_settled_only and open_res == 0.0:
                        time.sleep(4.5)
                        _, confirm_today = client.get("/api/v1/dalton/budget/today")
                        confirm_settled = float(confirm_today.get("settled_usd") or 0.0)
                        confirm_open = float(confirm_today.get("open_reserved_usd") or 0.0)
                        after_turn_today = confirm_today
                        if confirm_open == 0.0 and abs(confirm_settled - new_settled_only) < 1e-9:
                            settled_confirmed = True
                            break

                if not settled_confirmed:
                    raise TimeoutError(
                        f"Timed out after 120s waiting for budget settlement after subagent turn {turn_idx}: {after_turn_today}"
                    )

                subagent_turns.append(
                    {
                        "turn_index": turn_idx,
                        "sse_summary": sub_summary,
                        "spent_usd_after_turn": after_turn_today.get("spent_usd"),
                        "settled_usd_after_turn": after_turn_today.get("settled_usd"),
                        "open_reserved_usd_after_turn": after_turn_today.get("open_reserved_usd"),
                        "remaining_usd_after_turn": after_turn_today.get("remaining_usd"),
                    }
                )
                if _gate_would_refuse(after_turn_today, floor_usd) and float(
                    after_turn_today.get("open_reserved_usd") or 0.0
                ) == 0.0:
                    reached_cap = True
                    break

            if not reached_cap:
                raise TimeoutError(
                    f"Timed out after 600s waiting for cap_usd={cap_usd} - spent_usd < admission_floor_usd={floor_usd} "
                    f"(turns={turn_idx})"
                )

            assert any(
                tc.get("name") == "Task"
                for turn in subagent_turns
                for batch in turn["sse_summary"]["tool_calls"]
                for tc in batch["tool_calls"]
            ), (
                f"No cash subagent (`Task`) turn ran before the admission floor was reached (cap_usd={cap_usd}, "
                f"floor_usd={floor_usd}, turns={turn_idx}); raise --cap-usd"
            )

            _, budget_at_cap = client.get("/api/v1/dalton/budget/today")
            assert _gate_would_refuse(budget_at_cap, floor_usd), (
                f"Expected cap_usd - spent_usd < admission_floor_usd {floor_usd} before rejection turn, "
                f"got {budget_at_cap}"
            )

            # 6. Next turn on `POST /api/v1/threads/{thread_id}/messages` -> rejected with HTTP 429
            rejected_status, rejected_events, rejected_body = client.stream_sse(
                "POST",
                f"/api/v1/threads/{thread_id}/messages",
                json_body={
                    "llm_model": "claude-sonnet-5-5-low-oauth",
                    "messages": [
                        {
                            "role": "user",
                            "content": "继续下一轮分析（预期因日现金额度耗尽被 429 拒绝）。",
                        }
                    ],
                },
            )
            assert rejected_status == 429, (
                f"Expected HTTP 429 on budget-exhausted turn, got {rejected_status}: {rejected_body}"
            )
            detail = (rejected_body or {}).get("detail") or {}
            assert detail.get("type") == "dalton_daily_budget", (
                f"Expected detail.type == 'dalton_daily_budget', got {rejected_body}"
            )

            screenshot_path = (
                REPO_ROOT
                / "var"
                / "dalton"
                / "trial-runs"
                / f"realrun-{args.date}"
                / "g1_budget_exhausted_banner.png"
            )
            screenshot_info, screenshot_bytes = _capture_rejection_banner_screenshot(
                output_path=screenshot_path,
                frontend_url=args.frontend,
                backend_url=args.backend,
                user_id=user_a,
                workspace_id=workspace_id,
                thread_id=thread_id,
            )

        # 7. Two-user budget isolation: while user_a (`realrun-pm-a`) is rejected with HTTP 429,
        #    user_b (`realrun-user-b`) configures a cash BYOK key (`PUT /api/v1/users/me/api-keys`),
        #    runs a `deepseek-flash` conversation turn, and succeeds with HTTP 200 (P0J §8.1 #19).
        with RealRunHttpClient(base_url=args.backend, user_id=user_b, timeout_s=180.0) as client_b:
            client_b.ensure_user()
            st_prior_b, body_prior_b = client_b.get("/api/v1/dalton/budget")
            if st_prior_b == 200 and isinstance(body_prior_b, dict):
                prior_budget_b = body_prior_b
                was_configured_b = bool(body_prior_b.get("configured"))

            prior_keys_b = _snapshot_api_keys_state(client_b)

            st_init_today_b, init_today_b = client_b.get("/api/v1/dalton/budget/today")
            assert st_init_today_b == 200 and isinstance(init_today_b, dict), (
                f"GET /api/v1/dalton/budget/today failed for {user_b}: {st_init_today_b} {init_today_b}"
            )
            baseline_spent_b = float(init_today_b.get("spent_usd") or 0.0)
            user_b_cap_usd = round(baseline_spent_b + user_b_cap_delta_usd, 6)

            st_b_cfg, b_cfg = client_b.put(
                "/api/v1/dalton/budget",
                json_body={"daily_cap_usd": user_b_cap_usd, "default_task_cap_usd": user_b_cap_delta_usd},
            )
            assert st_b_cfg == 200, f"user_b PUT /api/v1/dalton/budget failed: {st_b_cfg} {b_cfg}"
            budget_modified_b = True

            key_modified_b = _ensure_deepseek_byok_for_g1(client_b, prior_keys_b)

            st_b_ws, b_ws = client_b.post(
                "/api/v1/workspaces",
                json_body={
                    "name": "GOOGL-B",
                    "description": "G1 user_b isolation workspace",
                    "config": build_realrun_workspace_config("g1_budget.py"),
                },
            )
            assert st_b_ws in (200, 201) and b_ws.get("workspace_id")
            ws_b_id = str(b_ws["workspace_id"])
            st_b_turn, b_events, b_err = client_b.stream_sse(
                "POST",
                "/api/v1/threads/messages",
                json_body={
                    "workspace_id": ws_b_id,
                    "llm_model": "deepseek-flash",
                    "messages": [
                        {
                            "role": "user",
                            "content": "不要调用任何工具，仅用一句话确认你收到了本条消息。",
                        }
                    ],
                },
                max_events=500,
            )
            assert st_b_turn == 200 and b_err is None, (
                f"Expected user_b conversation turn to succeed with 200 while user_a is at 429, got {st_b_turn}: {b_err}"
            )
            b_turn_summary = _extract_sse_summary(b_events)
            time.sleep(1.0)
            _, b_today = client_b.get("/api/v1/dalton/budget/today")
            b_remaining = float(b_today.get("remaining_usd") or 0.0)
            assert 0.0 < b_remaining < user_b_cap_delta_usd, (
                f"Expected 0.0 < user_b remaining_usd < {user_b_cap_delta_usd}, got {b_today}"
            )
    finally:
        cleanup_result = _cleanup_g1_state(
            backend_url=args.backend,
            user_a=user_a,
            workspace_id=workspace_id,
            subagent_modified_a=subagent_modified_a,
            prior_subagent_a=prior_subagent_a,
            key_modified_a=key_modified_a,
            prior_keys_a=prior_keys_a,
            budget_modified_a=budget_modified_a,
            was_configured_a=was_configured_a,
            prior_budget_a=prior_budget_a,
            user_b=user_b,
            ws_b_id=ws_b_id,
            key_modified_b=key_modified_b,
            prior_keys_b=prior_keys_b,
            budget_modified_b=budget_modified_b,
            was_configured_b=was_configured_b,
            prior_budget_b=prior_budget_b,
        )

    added_spent_a, added_spent_b, total_added_spent_usd = _assert_g1_added_cash_spend(
        baseline_spent_a=baseline_spent_a,
        final_spent_a=float(budget_at_cap.get("spent_usd") or 0.0),
        baseline_spent_b=baseline_spent_b,
        final_spent_b=float(b_today.get("spent_usd") or 0.0),
    )
    byok_evidence = _build_g1_byok_evidence_fields(
        key_modified_a=key_modified_a,
        key_modified_b=key_modified_b,
        cleanup_result=cleanup_result,
    )

    ended_at = now_utc_iso()
    script_cmd = f"scripts/dalton/realrun/g1_budget.py --cap-usd {cap_delta_usd}"

    synthetic: list[dict[str, Any]] = [
        {
            "kind": "settings_api_budget",
            "entry_point": "PUT /api/v1/dalton/budget",
            "description": (
                f"通过设置接口为测试用户 A ({user_a}) 设低日额度 ${cap_usd:.4f}（当日已有现金花费 ${baseline_spent_a:.4f} + ${cap_delta_usd:.2f}）、"
                f"为用户 B ({user_b}) 设日额度 ${user_b_cap_usd:.4f}（当日已有现金花费 ${baseline_spent_b:.4f} + ${user_b_cap_delta_usd:.2f}），"
                "验证 A 触顶被 HTTP 429 拒绝的同时 B 正常调用不受影响，并在结束时恢复双方原预算、API Key 与子智能体配置。"
            ),
        }
    ]

    payload = {
        "test_user_ids": [user_a, user_b],
        "scenario_1_low_cap_subagent_and_429_rejection": {
            "user_id": user_a,
            "thread_id": thread_id,
            "workspace_id": workspace_id,
            "lead_model": "claude-sonnet-5-5-low-oauth",
            "cash_subagent_model": "deepseek-flash",
            "baseline_spent_usd": baseline_spent_a,
            "cap_delta_usd": cap_delta_usd,
            "configured_daily_cap_usd": cap_usd,
            "added_spent_usd": added_spent_a,
            "subagent_turns": subagent_turns,
            "budget_today_at_cap": {
                k: v
                for k, v in budget_at_cap.items()
                if k not in ("_raw_rows", "_decimals")
            },
            "rejected_next_turn": {
                "endpoint": f"POST /api/v1/threads/{thread_id}/messages",
                "http_status": rejected_status,
                "response_body": rejected_body,
            },
            "frontend_rejection_screenshot": screenshot_info,
            "budget_restored": cleanup_result.get("restore_err_a") is None,
            "api_keys_restored": byok_evidence["scenario_1_api_keys_restored"],
            "subagent_restored": cleanup_result.get("restore_subagent_err_a") is None,
        },
        "scenario_2_settings_byok_cash_key_counted": {
            "user_id": user_a,
            "settings_endpoint": byok_evidence["scenario_2_settings_endpoint"],
            "byok_provider": "deepseek",
            "temporary_key_written": byok_evidence["scenario_2_temporary_key_written"],
            "temporary_key_deleted": byok_evidence["scenario_2_temporary_key_deleted"],
            "key_write_skipped_reason": byok_evidence["scenario_2_key_write_skipped_reason"],
            "spent_usd_before_probe": today_before_probe.get("spent_usd"),
            "probe_response": probe_resp,
            "spent_usd_after_probe": today_after_probe.get("spent_usd"),
            "journal_cash_usd_after_probe": today_after_probe.get("journal_cash_usd"),
            "by_provider_after_probe": today_after_probe.get("by_provider"),
        },
        "scenario_3_review_second_call_cache_hit": {
            "user_id": user_a,
            "draft_path": "drafts/g1_review_cache.md",
            "publish_requests": [
                {
                    "publish_request_id": pr["publish_request_id"],
                    "slug": pr["slug"],
                    "status": pr["status"],
                    "reviewer_model": (pr.get("review") or {}).get("model"),
                    "reviewer_family": (pr.get("review") or {}).get("family"),
                    "verdict": (pr.get("review") or {}).get("verdict"),
                    "usage": (pr.get("review") or {}).get("usage"),
                }
                for pr in pub_requests[:2]
            ],
        },
        "scenario_4_two_user_budget_isolation": {
            "user_a": user_a,
            "user_a_http_status": rejected_status,
            "user_b": user_b,
            "user_b_model": "deepseek-flash",
            "user_b_baseline_spent_usd": baseline_spent_b,
            "user_b_cap_delta_usd": user_b_cap_delta_usd,
            "user_b_cap_usd": user_b_cap_usd,
            "user_b_added_spent_usd": added_spent_b,
            "total_added_spent_usd": total_added_spent_usd,
            "user_b_http_status": st_b_turn,
            "user_b_thread_id": b_turn_summary.get("thread_id"),
            "user_b_reply_preview": b_turn_summary.get("assistant_reply_preview"),
            "user_b_budget_today": {
                k: v for k, v in b_today.items() if k not in ("_raw_rows", "_decimals")
            },
            "user_b_temporary_key_written": byok_evidence["user_b_temporary_key_written"],
            "user_b_key_deleted": byok_evidence["user_b_key_deleted"],
            "user_b_key_write_skipped_reason": byok_evidence["user_b_key_write_skipped_reason"],
            "user_b_budget_restored": cleanup_result.get("restore_err_b") is None,
        },
    }

    envelope = build_evidence_envelope(
        script=script_cmd,
        started_at=started_at,
        ended_at=ended_at,
        backend=args.backend,
        test_user_id=test_user_id,
        synthetic=synthetic,
        extra_fields=payload,
    )

    conv_usages = (envelope.get("journal") or {}).get("conversation_usages") or []
    deepseek_conv_rows = [
        cu
        for cu in conv_usages
        if int(
            (
                (((cu.get("token_usage_summary") or {}).get("by_model") or {}).get("deepseek-flash") or {}).get(
                    "total_tokens"
                )
            )
            or 0
        )
        > 0
        or any(
            pc.get("model") == "deepseek-flash"
            and (int(pc.get("input_tokens") or 0) + int(pc.get("output_tokens") or 0)) > 0
            for pc in ((cu.get("token_usage_summary") or {}).get("per_call_costs") or [])
        )
    ]
    assert deepseek_conv_rows, (
        f"Expected at least one conversation_usages row for deepseek-flash with tokens > 0, got {conv_usages}"
    )
    combined_cash_usd = float(
        ((envelope.get("journal") or {}).get("totals") or {}).get("combined_cash_usd") or 0.0
    )
    assert combined_cash_usd <= 0.50, (
        f"Expected G1 total combined_cash_usd <= 0.50 USD, got ${combined_cash_usd:.6f}"
    )

    json_path = REPO_ROOT / "var" / "dalton" / "trial-runs" / f"realrun-{args.date}" / "g1_budget.json"
    md_path = (
        REPO_ROOT
        / "var"
        / "dalton"
        / "trial-runs"
        / f"realrun-{args.date}"
        / "G1.md"
    )

    key_metrics = {
        "测试用户 A / B": f"`{user_a}`（日额度设为已花现金 ${baseline_spent_a:.4f} + ${cap_delta_usd:.2f} = ${cap_usd:.4f}） / `{user_b}`（日额度 ${user_b_cap_usd:.2f}）",
        "主模型 (`lead_model`)": "claude-sonnet-5-5-low-oauth（Claude 订阅，现金成本 $0）",
        "现金子智能体 (`subagent:general-purpose`)": f"deepseek-flash（会话用量记录数：{len(deepseek_conv_rows)}，总花费计入 settled_usd=${float(budget_at_cap.get('settled_usd') or 0.0):.6f}）",
        "设置页 BYOK 现金 Key": f"{byok_evidence['byok_a_action_label']}；deepseek 探测花费 ${float(probe_resp.get('cash_usd') or 0.0):.6f} 计入 GET /api/v1/dalton/budget/today",
        "用户 A 触顶时日累计现金花费 (`spent_usd`)": f"${float(budget_at_cap.get('spent_usd') or 0.0):.6f}（剩余额度 ${float(budget_at_cap.get('remaining_usd') or 0.0):.6f}）",
        "本轮总现金花费 (`combined_cash_usd <= $0.50`)": f"${combined_cash_usd:.6f}",
        "用户 A 下一轮对话拒绝 (`POST /api/v1/threads/{id}/messages`)": f"HTTP {rejected_status}，detail.type = '{detail.get('type')}'",
        "用户 B 并行对话请求 (`POST /api/v1/threads/messages`, `deepseek-flash`)": f"HTTP {st_b_turn}（正常通过，剩余额度 ${float(b_today.get('remaining_usd') or 0.0):.4f}）",
        "前端拒绝提示截图": screenshot_info["screenshot_path"],
        "前端拒绝提示截图 `sha256`": screenshot_info["sha256"],
        "审稿第二次命中缓存 (`publish.review`)": (
            f"第一次 replayed={first_rev.get('replayed')} (invocation_id={first_rev.get('invocation_id')})，"
            f"第二次 replayed={second_rev.get('replayed')}, cached={second_rev.get('cached')}, cash_usd=${float(second_rev.get('cash_usd') or 0.0):.6f}"
        ),
        "运行后状态恢复": byok_evidence["restoration_summary"],
    }

    notes = [
        f"用户 A (`{user_a}`) 被拒那一轮（`POST /api/v1/threads/{thread_id}/messages`）完整返回体：`{json.dumps(rejected_body, ensure_ascii=False)}`",
        f"用户 B (`{user_b}`) 在用户 A 被 429 拒绝的同时使用现金 BYOK key 发起 `deepseek-flash` 对话（`thread_id={b_turn_summary.get('thread_id')}`），返回 `HTTP {st_b_turn}`，证明日预算闸门按 `user_id` 严格隔离；{byok_evidence['user_b_cleanup_note']}",
        f"前端拒绝提示横幅截图保存在 `{screenshot_info['screenshot_path']}`（`{screenshot_info['bytes']}` bytes，`sha256={screenshot_info['sha256']}`）。",
    ]

    written_raw, written_md, sha = write_evidence_and_summary(
        family_code="G1",
        title="预算闸门、现金子智能体花费、BYOK 现金计入与双用户隔离真实运行证据（P0J §5 G1）",
        raw_json_path=json_path,
        summary_md_path=md_path,
        envelope=envelope,
        key_findings=list(key_metrics.items()),
        notes=notes,
        record=args.record,
    )
    if args.record:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_bytes(screenshot_bytes)
    print(f"G1 evidence (record={args.record}): {written_raw} (sha256={sha})")
    print(f"G1 summary  (record={args.record}): {written_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
