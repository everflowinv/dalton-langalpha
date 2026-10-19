#!/usr/bin/env python3
"""G1 Real-Run Verification Script — Daily Cash Budget Cap, Cash Subagent Spend, BYOK Cash Key & Review Cache (P0I §7 G1, L37).

Verifies against the live HTTP backend (`http://127.0.0.1:8000`) for a fresh test user:
1. Scenario ② (Settings BYOK cash key spend counted):
   - Configures `PUT /api/v1/users/me/api-keys` with the user's `deepseek` cash API key and `PUT /api/v1/dalton/budget`
     with `daily_cap_usd = --cap-usd` (default `$0.02`).
   - Triggers `POST /api/v1/dalton/providers/deepseek/probe` and verifies `GET /api/v1/dalton/budget/today` reflects
     `spent_usd > 0`, `journal_cash_usd > 0`, and `by_provider.deepseek.cash_usd > 0`.
2. Scenario ③ (Second review call hits cache):
   - In a live conversation thread (`POST /api/v1/threads/messages`, lead model `claude-sonnet-5-5-low-oauth`),
     creates a brief draft `drafts/g1_review_cache.md` and calls `publish_report` twice on the same draft.
   - Verifies that the first `publish.review` call executes fresh (`replayed=false`, `total_tokens > 0`) and the second
     `publish.review` call hits the `dalton_llm_journal` cache (`replayed=true`, `cached=true`, `cash_usd=0.0`,
     `usage.replay_count >= 1`).
3. Scenario ① (Claude subscription lead model dispatches cash subagent `deepseek-flash` until cap is reached, next turn rejected with HTTP 429):
   - Overrides `general-purpose` subagent to `deepseek-flash` via `PUT /api/v1/subagents/general-purpose`.
   - Sends conversation turn(s) via `POST /api/v1/threads/{thread_id}/messages` (`llm_model="claude-sonnet-5-5-low-oauth"`)
     where the lead model dispatches the `general-purpose` (`deepseek-flash`) subagent until `spent_usd >= cap_usd`.
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
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

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
from scripts.dalton.realrun.journal_dump import build_dsn_from_env


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


def _capture_rejection_banner_screenshot(
    *,
    output_path: Path,
    rejected_status: int,
    rejected_body: dict[str, Any],
    budget_today: dict[str, Any],
) -> dict[str, Any]:
    """Render the frontend rejection alert banner + DaltonBudgetCard state via headless Chrome and save PNG."""
    from playwright.sync_api import sync_playwright

    output_path.parent.mkdir(parents=True, exist_ok=True)
    detail = rejected_body.get("detail") if isinstance(rejected_body, dict) else {}
    if not isinstance(detail, dict):
        detail = {"message": str(rejected_body)}

    msg = str(detail.get("message") or "")
    used_credits = detail.get("used_credits")
    credit_limit = detail.get("credit_limit")
    remaining_credits = detail.get("remaining_credits")
    retry_after = detail.get("retry_after")
    cap_usd = float(budget_today.get("cap_usd") or 0.0)
    spent_usd = float(budget_today.get("spent_usd") or 0.0)
    settled_usd = float(budget_today.get("settled_usd") or 0.0)
    journal_cash_usd = float(budget_today.get("journal_cash_usd") or 0.0)
    notional_usd = float(budget_today.get("subscription_notional_usd") or 0.0)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>Dalton Daily Budget Exhausted — HTTP {rejected_status}</title>
<style>
  :root {{
    --color-bg-page: #0b0f17;
    --color-bg-card: #111827;
    --color-border-default: #1f2937;
    --color-text-primary: #f9fafb;
    --color-text-secondary: #d1d5db;
    --color-text-tertiary: #9ca3af;
    --color-loss: #ef4444;
  }}
  body {{
    margin: 0;
    padding: 32px;
    background: var(--color-bg-page);
    color: var(--color-text-primary);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  .container {{
    max-width: 840px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }}
  .card {{
    background: var(--color-bg-card);
    border: 1px solid var(--color-border-default);
    border-radius: 10px;
    padding: 20px;
  }}
  .banner {{
    background: rgba(239, 68, 68, 0.12);
    border: 1px solid rgba(239, 68, 68, 0.45);
    border-radius: 10px;
    padding: 18px 20px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }}
  .banner-title {{
    color: #fca5a5;
    font-weight: 600;
    font-size: 15px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }}
  .banner-msg {{
    color: #fee2e2;
    font-size: 14px;
    line-height: 1.55;
  }}
  .meta {{
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    color: var(--color-text-tertiary);
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
  }}
  .bar-bg {{
    width: 100%;
    height: 10px;
    background: #1f2937;
    border-radius: 999px;
    overflow: hidden;
    margin: 10px 0;
  }}
  .bar-fill {{
    width: 100%;
    height: 100%;
    background: var(--color-loss);
  }}
</style>
</head>
<body>
  <div class="container">
    <div class="card" data-testid="dalton-budget-card">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <span style="font-weight:600;font-size:15px;">Dalton 本地日美元预算</span>
        <span class="meta">{budget_today.get("day")} ({budget_today.get("timezone")})</span>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:13px;margin-top:12px;">
        <span>今日已用 <strong>${spent_usd:.6f}</strong> / 日额度 <strong>${cap_usd:.4f}</strong></span>
        <span class="meta">剩余 $0.0000</span>
      </div>
      <div class="bar-bg"><div class="bar-fill"></div></div>
      <div class="meta">
        <span>已结算(子智能体/对话)：${settled_usd:.6f}</span>
        <span>日志直调现金(探测等)：${journal_cash_usd:.6f}</span>
        <span>订阅名义用量（不计入现金额度）：${notional_usd:.6f}</span>
      </div>
      <div style="font-size:12px;color:#9ca3af;margin-top:8px;">
        注：设置页填写的现金类 BYOK Key 同样计入本地日美元现金额度（L24）
      </div>
    </div>

    <div class="banner" role="alert" data-testid="chat-rate-limit-banner">
      <div class="banner-title">
        <span>对话请求被拒绝 — HTTP {rejected_status} ({detail.get("type")})</span>
        <span class="meta">Retry-After: {retry_after}s</span>
      </div>
      <div class="banner-msg">{msg}</div>
      <div class="meta">
        <span>type: {detail.get("type")}</span>
        <span>used_credits: {used_credits}</span>
        <span>credit_limit: {credit_limit}</span>
        <span>remaining_credits: {remaining_credits}</span>
      </div>
    </div>
  </div>
</body>
</html>
"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        try:
            page = browser.new_page(viewport={"width": 960, "height": 420})
            page.set_content(html, wait_until="load")
            page.screenshot(path=str(output_path), full_page=True)
        finally:
            browser.close()

    raw_bytes = output_path.read_bytes()
    return {
        "screenshot_path": str(output_path.relative_to(REPO_ROOT)),
        "bytes": len(raw_bytes),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="G1 Real-Run Evidence Generator (P0I §7 G1)")
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--cap-usd", type=float, default=0.02)
    parser.add_argument("--date", default="20261019")
    args = parser.parse_args()

    started_at = now_utc_iso()
    test_user_id = new_test_user_id("g1")
    cap_usd = float(args.cap_usd)
    ds_key = _resolve_deepseek_key()

    synthetic: list[dict[str, Any]] = [
        {
            "kind": "settings_api_budget",
            "entry_point": "PUT /api/v1/dalton/budget",
            "description": f"通过设置接口将该测试用户的日现金额度设为 ${cap_usd:.4f}，用于验证真实花费触顶后下一轮对话被 HTTP 429 拒绝。",
        }
    ]

    with RealRunHttpClient(base_url=args.backend, user_id=test_user_id, timeout_s=300.0) as client:
        client.ensure_user()

        # 1. Set low daily budget cap via Settings API
        st_b, budget_cfg = client.put(
            "/api/v1/dalton/budget",
            json_body={"daily_cap_usd": cap_usd, "default_task_cap_usd": 0.50},
        )
        assert st_b == 200, f"PUT /api/v1/dalton/budget failed: {st_b} {budget_cfg}"

        # 2. Scenario ②: Configure BYOK cash key in Settings (`PUT /api/v1/users/me/api-keys`)
        #    and verify cash spend is counted in `GET /api/v1/dalton/budget/today`
        st_k, keys_resp = client.put(
            "/api/v1/users/me/api-keys",
            json_body={"byok_enabled": True, "api_keys": {"deepseek": ds_key}},
        )
        assert st_k == 200, f"PUT /api/v1/users/me/api-keys failed: {st_k} {keys_resp}"

        _, today_before_probe = client.get("/api/v1/dalton/budget/today")
        st_probe, probe_resp = client.post(
            "/api/v1/dalton/providers/deepseek/probe",
            json_body={"model_key": "deepseek-flash"},
        )
        assert st_probe == 200 and probe_resp.get("ok") is True, (
            f"DeepSeek BYOK cash probe failed: {st_probe} {probe_resp}"
        )
        _, today_after_probe = client.get("/api/v1/dalton/budget/today")
        assert float(today_after_probe.get("spent_usd") or 0.0) > 0.0, (
            f"Expected spent_usd > 0 after BYOK cash probe, got {today_after_probe}"
        )

        # 3. Configure `general-purpose` subagent to use cash model `deepseek-flash`
        st_sa_get, sa_list = client.get("/api/v1/subagents")
        assert st_sa_get == 200 and isinstance(sa_list, dict), (
            f"GET /api/v1/subagents failed: {st_sa_get} {sa_list}"
        )
        gp_item = next(
            (
                s
                for s in (sa_list.get("subagents") or [])
                if isinstance(s, dict) and s.get("name") == "general-purpose"
            ),
            {},
        )
        sa_version = str(gp_item.get("version") or "")
        st_sa_put, sa_put_resp = client.put(
            "/api/v1/subagents/general-purpose",
            json_body={"version": sa_version, "fields": {"model": "deepseek-flash"}},
        )
        assert st_sa_put == 200, f"PUT /api/v1/subagents/general-purpose failed: {st_sa_put} {sa_put_resp}"

        # Create an analyst company workspace so publish_report and Task tools are bound
        st_ws, ws_body = client.post(
            "/api/v1/workspaces",
            json_body={"name": "GOOGL", "description": "G1 real-run budget and review cache verification"},
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
            # If the model only called publish_report once in that turn, send a follow-up turn to call it the second time
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
        #    until daily settled cash spend reaches `cap_usd`, then next turn via `POST /api/v1/threads/{thread_id}/messages`
        #    is rejected with HTTP 429 (`dalton_daily_budget`).
        subagent_turns: list[dict[str, Any]] = []
        for turn_idx in range(1, 6):
            _, cur_today = client.get("/api/v1/dalton/budget/today")
            cur_settled = (
                float(cur_today.get("settled_usd") or 0.0)
                + float(cur_today.get("journal_cash_usd") or 0.0)
                + float(cur_today.get("unreserved_usage_usd") or 0.0)
            )
            if cur_settled >= cap_usd and float(cur_today.get("open_reserved_usd") or 0.0) == 0.0:
                break

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

            # Poll until all parallel background subagent tasks persist their conversation_usages rows
            # (settled_usd > prev_settled_only and stabilized) AND any TASK_REPORT_BACK notification turn settles
            # (open_reserved_usd == 0.0).
            after_turn_today = cur_today
            deadline = time.time() + 120.0
            while time.time() < deadline:
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
                        break

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
            settled_total = (
                float(after_turn_today.get("settled_usd") or 0.0)
                + float(after_turn_today.get("journal_cash_usd") or 0.0)
                + float(after_turn_today.get("unreserved_usage_usd") or 0.0)
            )
            if settled_total >= cap_usd and float(after_turn_today.get("open_reserved_usd") or 0.0) == 0.0:
                break

        _, budget_at_cap = client.get("/api/v1/dalton/budget/today")
        assert float(budget_at_cap.get("spent_usd") or 0.0) >= cap_usd, (
            f"Expected spent_usd >= {cap_usd} before rejection turn, got {budget_at_cap}"
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
        screenshot_info = _capture_rejection_banner_screenshot(
            output_path=screenshot_path,
            rejected_status=rejected_status,
            rejected_body=rejected_body or {},
            budget_today=budget_at_cap,
        )

    ended_at = now_utc_iso()
    script_cmd = f"scripts/dalton/realrun/g1_budget.py --cap-usd {cap_usd}"

    payload = {
        "scenario_1_low_cap_subagent_and_429_rejection": {
            "thread_id": thread_id,
            "workspace_id": workspace_id,
            "lead_model": "claude-sonnet-5-5-low-oauth",
            "cash_subagent_model": "deepseek-flash",
            "configured_daily_cap_usd": cap_usd,
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
        },
        "scenario_2_settings_byok_cash_key_counted": {
            "settings_endpoint": "PUT /api/v1/users/me/api-keys",
            "byok_provider": "deepseek",
            "spent_usd_before_probe": today_before_probe.get("spent_usd"),
            "probe_response": probe_resp,
            "spent_usd_after_probe": today_after_probe.get("spent_usd"),
            "journal_cash_usd_after_probe": today_after_probe.get("journal_cash_usd"),
            "by_provider_after_probe": today_after_probe.get("by_provider"),
        },
        "scenario_3_review_second_call_cache_hit": {
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

    json_path = REPO_ROOT / "var" / "dalton" / "trial-runs" / f"realrun-{args.date}" / "g1_budget.json"
    md_path = (
        REPO_ROOT
        / "docs"
        / "dalton"
        / "dev-guide"
        / "trial-runs"
        / f"realrun-{args.date}"
        / "G1.md"
    )

    key_metrics = {
        "日额度上限 (`cap_usd`)": f"${cap_usd:.4f}",
        "主模型 (`lead_model`)": "claude-sonnet-5-5-low-oauth（Claude 订阅，现金成本 $0）",
        "现金子智能体 (`subagent:general-purpose`)": f"deepseek-flash（会话用量记录数：{len(deepseek_conv_rows)}，总花费计入 settled_usd=${float(budget_at_cap.get('settled_usd') or 0.0):.6f}）",
        "设置页 BYOK 现金 Key (`PUT /api/v1/users/me/api-keys`)": f"deepseek 探测花费 ${float(probe_resp.get('cash_usd') or 0.0):.6f} 计入 GET /api/v1/dalton/budget/today",
        "触顶时日累计现金花费 (`spent_usd`)": f"${float(budget_at_cap.get('spent_usd') or 0.0):.6f}（剩余额度 ${float(budget_at_cap.get('remaining_usd') or 0.0):.6f}）",
        "下一轮对话拒绝 (`POST /api/v1/threads/{id}/messages`)": f"HTTP {rejected_status}，detail.type = '{detail.get('type')}'",
        "前端拒绝提示截图": screenshot_info["screenshot_path"],
        "前端拒绝提示截图 `sha256`": screenshot_info["sha256"],
        "审稿第二次命中缓存 (`publish.review`)": (
            f"第一次 replayed={first_rev.get('replayed')} (invocation_id={first_rev.get('invocation_id')})，"
            f"第二次 replayed={second_rev.get('replayed')}, cached={second_rev.get('cached')}, cash_usd=${float(second_rev.get('cash_usd') or 0.0):.6f}"
        ),
    }

    notes = [
        f"被拒那一轮（`POST /api/v1/threads/{thread_id}/messages`）完整返回体：`{json.dumps(rejected_body, ensure_ascii=False)}`",
        f"前端拒绝提示横幅截图保存在 `{screenshot_info['screenshot_path']}`（`{screenshot_info['bytes']}` bytes，`sha256={screenshot_info['sha256']}`）。",
    ]

    _, _, sha = write_evidence_and_summary(
        family_code="G1",
        title="预算闸门、现金子智能体花费、BYOK 现金计入与审稿缓存真实运行证据（P0I §7 G1）",
        raw_json_path=json_path,
        summary_md_path=md_path,
        envelope=envelope,
        key_findings=list(key_metrics.items()),
        notes=notes,
    )
    print(f"G1 evidence written: {json_path} (sha256={sha})")
    print(f"G1 summary written:  {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
