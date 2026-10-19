"""Weekly autonomy health & verdict distribution report generator (PK.20)."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row

from src.config.env import LOCAL_DEV_USER_ID
from src.dalton.budget.service import get_budget_today
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

FIVE_VERDICTS = (
    "NO_CHANGE",
    "THESIS_STRENGTHENED",
    "THESIS_WEAKENED",
    "THESIS_BROKEN",
    "NEW_THESIS",
)


async def generate_weekly_autonomy_report(
    days: int = 7,
    *,
    user_id: str | None = None,
    week: str | None = None,
) -> dict[str, Any]:
    """Compile a 7-day autonomy report across preflight logs, signals, verdicts, and publications."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    preflight_total = 0
    preflight_skipped = 0
    preflight_woken = 0
    skip_reasons: Counter[str] = Counter()
    signals_detected = 0
    signals_woken = 0
    signals_resolved = 0
    signals_expired = 0
    verdict_counts: Counter[str] = Counter()
    verdicts_by_ticker: dict[str, Counter[str]] = {}
    publications_count = 0
    pending_approvals_count = 0

    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT decision, reason
                    FROM dalton_preflight_log
                    WHERE checked_at >= %s
                    """,
                    (since,),
                )
                for row in await cur.fetchall():
                    preflight_total += 1
                    dec = str(row.get("decision") or "")
                    if dec == "skip":
                        preflight_skipped += 1
                        skip_reasons[str(row.get("reason") or "unknown")] += 1
                    elif dec == "wake":
                        preflight_woken += 1

                await cur.execute(
                    """
                    SELECT woken_execution_id, resolved_at, expired_at
                    FROM dalton_signals
                    WHERE detected_at >= %s
                    """,
                    (since,),
                )
                for row in await cur.fetchall():
                    signals_detected += 1
                    if row.get("woken_execution_id") is not None:
                        signals_woken += 1
                    if row.get("resolved_at") is not None:
                        signals_resolved += 1
                    elif row.get("expired_at") is not None:
                        signals_expired += 1

                await cur.execute(
                    """
                    SELECT workspace_id, ticker, word
                    FROM dalton_verdicts
                    WHERE verdict_at >= %s
                    """,
                    (since,),
                )
                for row in await cur.fetchall():
                    v = str(row.get("word") or "")
                    t = str(row.get("ticker") or row.get("workspace_id") or "UNKNOWN").upper()
                    verdict_counts[v] += 1
                    verdicts_by_ticker.setdefault(t, Counter())[v] += 1

                await cur.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM dalton_publications
                    WHERE created_at >= %s
                    """,
                    (since,),
                )
                pub_row = await cur.fetchone()
                publications_count = int((pub_row or {}).get("cnt") or 0)

                await cur.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM dalton_publish_requests
                    WHERE status = 'awaiting_pm'
                    """
                )
                req_row = await cur.fetchone()
                pending_approvals_count = int((req_row or {}).get("cnt") or 0)
    except Exception as e:
        logger.warning("[DALTON_WEEK_REPORT] DB query error: %s", e)

    budget: dict[str, Any] = {}
    try:
        budget = await get_budget_today(user_id=user_id or LOCAL_DEV_USER_ID)
    except Exception as e:
        logger.warning("[DALTON_WEEK_REPORT] get_budget_today failed: %s", e)

    skip_rate_pct = (
        round((preflight_skipped / preflight_total) * 100.0, 1)
        if preflight_total > 0
        else 0.0
    )

    lazy_tickers: list[str] = []
    for t, ctr in sorted(verdicts_by_ticker.items()):
        tot = sum(ctr.values())
        if tot >= 3 and ctr.get("NO_CHANGE", 0) == tot:
            lazy_tickers.append(t)

    md_lines = [
        f"# Dalton 自主运行周度体检报告（过去 {days} 天）",
        "",
        f"- 生成时间：`{now.isoformat()}`",
        f"- 预检总次数：**{preflight_total}**（零 token 跳过 **{preflight_skipped}** 次，唤醒 **{preflight_woken}** 次，跳过率 **{skip_rate_pct}%**）",
        f"- 信号统计：检测 **{signals_detected}** 条 · 已唤醒 **{signals_woken}** 条 · 已闭环 **{signals_resolved}** 条 · 超期归档 **{signals_expired}** 条",
        f"- 正式发布：**{publications_count}** 篇 · 待 PM 审批：**{pending_approvals_count}** 篇",
        "",
        "## 五词结论分布（Verdict Distribution）",
        "",
        "| 结论词 | 次数 |",
        "|---|---:|",
    ]
    for v_word in FIVE_VERDICTS:
        md_lines.append(f"| `{v_word}` | {verdict_counts.get(v_word, 0)} |")

    if lazy_tickers:
        md_lines.extend(
            [
                "",
                f"> [!WARNING] 连续 ≥3 次全部为 `NO_CHANGE` 的公司：{', '.join(lazy_tickers)}（建议 PM 抽检是否存在结论惰性）。",
            ]
        )

    return {
        "window_days": days,
        "week": week,
        "generated_at": now.isoformat(),
        "preflight": {
            "total": preflight_total,
            "skipped": preflight_skipped,
            "woken": preflight_woken,
            "skip_rate_pct": skip_rate_pct,
            "skip_reasons": dict(skip_reasons),
        },
        "signals": {
            "detected": signals_detected,
            "woken": signals_woken,
            "resolved": signals_resolved,
            "expired": signals_expired,
        },
        "verdicts": {k: verdict_counts.get(k, 0) for k in FIVE_VERDICTS},
        "lazy_verdict_tickers": lazy_tickers,
        "publications_count": publications_count,
        "pending_approvals_count": pending_approvals_count,
        "budget": budget,
        "markdown": "\n".join(md_lines) + "\n",
    }


build_weekly_autonomy_report = generate_weekly_autonomy_report
