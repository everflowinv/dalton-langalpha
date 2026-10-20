#!/usr/bin/env python3
"""Export `dalton_llm_journal` records for a test user and time window (P0I §2.1 #3, L37).

Usage:
    uv run python scripts/dalton/realrun/journal_dump.py \\
        --user <user_id> --since <ISO8601> --until <ISO8601> [--output <path.json>]

Never exports request or response bodies (`response` JSONB is strictly omitted).
Computes deterministic `totals` from the exported rows so evidence and reports
never hand-author call counts or USD spend.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")


def build_dsn_from_env(dsn: str | None = None) -> str:
    """Build Postgres connection string from `.env` without importing `src.dalton`."""
    if dsn:
        return dsn
    env_dsn = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if env_dsn:
        return env_dsn
    db_host = os.getenv("DB_HOST", "127.0.0.1")
    if db_host in ("postgres", "db", "host.docker.internal"):
        db_host = "127.0.0.1"
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME", "postgres")
    db_user = os.getenv("DB_USER", "postgres")
    db_password = os.getenv("DB_PASSWORD", "postgres")
    sslmode = os.getenv("DB_SSLMODE", "prefer")
    return (
        f"postgresql://{quote_plus(db_user)}:{quote_plus(db_password)}"
        f"@{db_host}:{db_port}/{db_name}?sslmode={sslmode}"
    )


def parse_iso_utc(ts_str: str) -> datetime:
    """Parse an ISO-8601 timestamp string into a timezone-aware UTC `datetime`."""
    cleaned = ts_str.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_iso_utc(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(dt)


def sanitize_usage_metadata(raw_usage: Any) -> dict[str, Any]:
    """Keep only numeric/token usage metadata; strip any text/body fields."""
    if not isinstance(raw_usage, dict):
        return {}
    allowed_keys = {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cached_tokens",
        "replayed",
        "cached",
        "replay_count",
        "latency_ms",
        "attempt",
    }
    cleaned: dict[str, Any] = {}
    for k, v in raw_usage.items():
        if k in allowed_keys or k.endswith("_tokens") or k.endswith("_usd") or k.endswith("_cost"):
            if isinstance(v, (int, float, bool, str)) or v is None:
                cleaned[k] = v
    return cleaned


def _extract_total_tokens(usage: dict[str, Any]) -> int:
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and total > 0:
        return int(total)
    inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    return inp + out


def normalize_journal_rows(
    raw_rows: list[dict[str, Any]],
    *,
    since_dt: datetime | None = None,
    conversation_rows: list[dict[str, Any]] | None = None,
    conversation_usages_unavailable: bool = False,
    window_user_ids: list[str] | None = None,
    unexpected_user_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Normalize raw `dalton_llm_journal` rows (and optional conversation usage rows) into `{rows, totals}`."""
    rows: list[dict[str, Any]] = []
    total_cash = Decimal("0")
    total_notional = Decimal("0")
    fresh_calls = 0
    replayed_calls = 0
    done_calls = 0
    failed_calls = 0
    indeterminate_calls = 0
    by_purpose: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}

    for r in raw_rows:
        usage = sanitize_usage_metadata(r.get("usage"))
        created_at = r.get("created_at")
        is_replayed = bool(r.get("replayed") or usage.get("replayed") or usage.get("cached"))
        if (
            not is_replayed
            and since_dt is not None
            and isinstance(created_at, datetime)
        ):
            c_utc = (
                created_at.replace(tzinfo=timezone.utc)
                if created_at.tzinfo is None
                else created_at.astimezone(timezone.utc)
            )
            if c_utc < since_dt:
                is_replayed = True

        cash_dec = Decimal(str(r.get("cash_usd") or "0"))
        notional_dec = Decimal(str(r.get("notional_usd") or "0"))
        state = str(r.get("state") or "")
        purpose = str(r.get("purpose") or "unknown")
        model_key = str(r.get("model_key") or "unknown")

        row_item = {
            "invocation_id": str(r.get("invocation_id") or ""),
            "user_id": r.get("user_id"),
            "scope_ref": str(r.get("scope_ref") or ""),
            "purpose": purpose,
            "model_key": model_key,
            "provider": r.get("provider"),
            "billing_type": r.get("billing_type"),
            "state": state,
            "attempt": int(r.get("attempt") or 1),
            "replayed": is_replayed,
            "cached": bool(r.get("cached") or is_replayed),
            "usage": usage,
            "total_tokens": _extract_total_tokens(usage),
            "cash_usd": float(cash_dec),
            "notional_usd": float(notional_dec),
            "error_kind": r.get("error_kind"),
            "claimed_at": format_iso_utc(r.get("claimed_at")),
            "completed_at": format_iso_utc(r.get("completed_at")),
            "created_at": format_iso_utc(created_at),
        }
        rows.append(row_item)

        if state in ("done", "indeterminate"):
            total_cash += cash_dec
            total_notional += notional_dec
        if state == "done":
            done_calls += 1
        elif state == "failed":
            failed_calls += 1
        elif state == "indeterminate":
            indeterminate_calls += 1

        if is_replayed:
            replayed_calls += 1
        else:
            fresh_calls += 1

        p_bucket = by_purpose.setdefault(
            purpose,
            {"calls": 0, "fresh_calls": 0, "replayed_calls": 0, "cash_usd": 0.0, "notional_usd": 0.0},
        )
        p_bucket["calls"] += 1
        if is_replayed:
            p_bucket["replayed_calls"] += 1
        else:
            p_bucket["fresh_calls"] += 1
        p_bucket["cash_usd"] = round(float(Decimal(str(p_bucket["cash_usd"])) + cash_dec), 6)
        p_bucket["notional_usd"] = round(
            float(Decimal(str(p_bucket["notional_usd"])) + notional_dec), 6
        )

        m_bucket = by_model.setdefault(
            model_key,
            {"calls": 0, "fresh_calls": 0, "replayed_calls": 0, "cash_usd": 0.0, "notional_usd": 0.0},
        )
        m_bucket["calls"] += 1
        if is_replayed:
            m_bucket["replayed_calls"] += 1
        else:
            m_bucket["fresh_calls"] += 1
        m_bucket["cash_usd"] = round(float(Decimal(str(m_bucket["cash_usd"])) + cash_dec), 6)
        m_bucket["notional_usd"] = round(
            float(Decimal(str(m_bucket["notional_usd"])) + notional_dec), 6
        )

        replay_cnt = int(usage.get("replay_count") or 0) if not is_replayed else 0
        for _ in range(replay_cnt):
            replay_ts = str(usage.get("last_replayed_at") or format_iso_utc(created_at))
            replay_item = {
                **row_item,
                "replayed": True,
                "cached": True,
                "cash_usd": 0.0,
                "notional_usd": 0.0,
                "created_at": replay_ts,
            }
            rows.append(replay_item)
            replayed_calls += 1
            if state == "done":
                done_calls += 1
            p_bucket["calls"] += 1
            p_bucket["replayed_calls"] += 1
            m_bucket["calls"] += 1
            m_bucket["replayed_calls"] += 1

    conv_items: list[dict[str, Any]] = []
    conv_cash = Decimal("0")
    if conversation_rows:
        for cr in conversation_rows:
            c_usd = Decimal(str(cr.get("cash_usd") or "0"))
            conv_cash += c_usd
            conv_items.append(
                {
                    "conversation_usage_id": str(cr.get("conversation_usage_id") or ""),
                    "conversation_response_id": str(cr.get("conversation_response_id") or ""),
                    "user_id": cr.get("user_id"),
                    "is_byok": bool(cr.get("is_byok")),
                    "cash_usd": float(c_usd),
                    "token_usage_summary": _summarize_conversation_token_usage(
                        cr.get("token_usage")
                    ),
                    "created_at": format_iso_utc(cr.get("created_at")),
                }
            )

    totals_dict: dict[str, Any] = {
        "total_calls": len(rows),
        "fresh_calls": fresh_calls,
        "replayed_calls": replayed_calls,
        "done_calls": done_calls,
        "failed_calls": failed_calls,
        "indeterminate_calls": indeterminate_calls,
        "cash_usd": float(round(total_cash, 6)),
        "notional_usd": float(round(total_notional, 6)),
        "conversation_turns": len(conv_items),
        "conversation_cash_usd": float(round(conv_cash, 6)),
        "combined_cash_usd": float(round(total_cash + conv_cash, 6)),
        "conversation_usages_unavailable": bool(conversation_usages_unavailable),
        "by_purpose": by_purpose,
        "by_model": by_model,
    }
    if window_user_ids is not None:
        totals_dict["window_user_ids"] = list(window_user_ids)
    if unexpected_user_ids is not None:
        totals_dict["unexpected_user_ids"] = list(unexpected_user_ids)

    result: dict[str, Any] = {
        "rows": rows,
        "totals": totals_dict,
    }
    if conv_items:
        result["conversation_usages"] = conv_items
    return result


def _summarize_conversation_token_usage(token_usage: Any) -> dict[str, Any]:
    """Extract per-call model/token/cost summary from `conversation_usages.token_usage` without bodies."""
    if not isinstance(token_usage, dict):
        return {}
    per_call = token_usage.get("per_call_costs")
    calls_out: list[dict[str, Any]] = []
    if isinstance(per_call, list):
        for entry in per_call:
            if not isinstance(entry, dict):
                continue
            calls_out.append(
                {
                    "model": entry.get("model") or entry.get("model_name") or entry.get("llm_model"),
                    "agent": entry.get("agent") or entry.get("role"),
                    "billing_type": entry.get("billing_type"),
                    "input_tokens": int(entry.get("input_tokens") or 0),
                    "output_tokens": int(entry.get("output_tokens") or 0),
                    "cost": float(entry.get("cost") or 0.0),
                }
            )
    return {
        "by_model": token_usage.get("by_model") or {},
        "per_call_costs": calls_out,
        "call_count": len(calls_out),
    }


def _normalize_user_id_list(user_id: str | list[str] | tuple[str, ...] | set[str]) -> list[str]:
    if isinstance(user_id, str):
        parts = [u.strip() for u in user_id.split(",") if u.strip()]
        return parts if parts else [user_id]
    return [str(u).strip() for u in user_id if str(u).strip()]


def dump_journal(
    *,
    user_id: str | list[str] | tuple[str, ...] | set[str],
    since: str | datetime,
    until: str | datetime,
    dsn: str | None = None,
    include_null_user: bool = False,
    include_conversation_usages: bool = True,
) -> dict[str, Any]:
    """Query `dalton_llm_journal` (and optionally `conversation_usages`) for `(user_id, [since, until])`."""
    from datetime import timedelta

    user_ids = _normalize_user_id_list(user_id)
    since_dt = parse_iso_utc(since) if isinstance(since, str) else since.astimezone(timezone.utc)
    until_dt = parse_iso_utc(until) if isinstance(until, str) else until.astimezone(timezone.utc)
    if until_dt.microsecond == 0:
        until_dt = until_dt + timedelta(seconds=2)
    conn_str = build_dsn_from_env(dsn)

    conv_unavailable = False
    window_uids: set[str] = set()

    with psycopg.connect(conn_str, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            if include_null_user:
                cur.execute(
                    """
                    SELECT
                        invocation_id, user_id, scope_ref, purpose, model_key,
                        provider, billing_type, state, attempt, usage,
                        cash_usd, notional_usd, error_kind,
                        claimed_at, completed_at, created_at
                    FROM dalton_llm_journal
                    WHERE created_at >= %s
                      AND created_at <= %s
                      AND (user_id = ANY(%s) OR user_id IS NULL)
                    ORDER BY created_at ASC, invocation_id ASC
                    """,
                    (since_dt, until_dt, user_ids),
                )
            else:
                cur.execute(
                    """
                    SELECT
                        invocation_id, user_id, scope_ref, purpose, model_key,
                        provider, billing_type, state, attempt, usage,
                        cash_usd, notional_usd, error_kind,
                        claimed_at, completed_at, created_at
                    FROM dalton_llm_journal
                    WHERE created_at >= %s
                      AND created_at <= %s
                      AND user_id = ANY(%s)
                    ORDER BY created_at ASC, invocation_id ASC
                    """,
                    (since_dt, until_dt, user_ids),
                )
            journal_rows = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT DISTINCT COALESCE(NULLIF(BTRIM(user_id), ''), '<NULL>') AS uid
                FROM dalton_llm_journal
                WHERE created_at >= %s
                  AND created_at <= %s
                """,
                (since_dt, until_dt),
            )
            for row in cur.fetchall():
                if row.get("uid"):
                    window_uids.add(str(row["uid"]))

            conv_rows: list[dict[str, Any]] = []
            if include_conversation_usages:
                try:
                    cur.execute(
                        """
                        SELECT
                            conversation_usage_id,
                            conversation_response_id,
                            user_id,
                            is_byok,
                            token_usage,
                            dalton_cash_usd(token_usage::jsonb, total_credits, infrastructure_credits, 1000) AS cash_usd,
                            created_at
                        FROM conversation_usages
                        WHERE user_id = ANY(%s)
                          AND created_at >= %s
                          AND created_at <= %s
                        ORDER BY created_at ASC
                        """,
                        (user_ids, since_dt, until_dt),
                    )
                    conv_rows = [dict(r) for r in cur.fetchall()]
                    cur.execute(
                        """
                        SELECT DISTINCT COALESCE(NULLIF(BTRIM(user_id), ''), '<NULL>') AS uid
                        FROM conversation_usages
                        WHERE created_at >= %s
                          AND created_at <= %s
                        """,
                        (since_dt, until_dt),
                    )
                    for row in cur.fetchall():
                        if row.get("uid"):
                            window_uids.add(str(row["uid"]))
                except Exception as exc:
                    conn.rollback()
                    conv_unavailable = True
                    sys.stderr.write(
                        f"WARNING: Failed to query conversation_usages for {user_ids}: {exc}\n"
                    )
                    conv_rows = []

    allowed_set = {u for u in user_ids if u and u != "<NULL>"}
    unexpected = sorted(uid for uid in window_uids if uid not in allowed_set)

    return normalize_journal_rows(
        journal_rows,
        since_dt=since_dt,
        conversation_rows=conv_rows,
        conversation_usages_unavailable=conv_unavailable,
        window_user_ids=sorted(window_uids),
        unexpected_user_ids=unexpected,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dump dalton_llm_journal records for user(s) and time window (without request/response bodies)."
    )
    parser.add_argument(
        "--user",
        required=True,
        help="Test user_id(s) to filter on (comma-separated for multi-user)",
    )
    parser.add_argument("--since", required=True, help="Window start ISO-8601 timestamp (UTC)")
    parser.add_argument("--until", required=True, help="Window end ISO-8601 timestamp (UTC)")
    parser.add_argument("--dsn", default=None, help="Optional Postgres DSN override")
    parser.add_argument(
        "--include-null-user",
        action="store_true",
        help="Also include rows where user_id IS NULL in the window",
    )
    parser.add_argument(
        "--no-conversation-usages",
        action="store_true",
        help="Skip querying conversation_usages for thread/subagent turns",
    )
    parser.add_argument("--output", default=None, help="Optional output JSON file path")
    args = parser.parse_args(argv)

    payload = dump_journal(
        user_id=args.user,
        since=args.since,
        until=args.until,
        dsn=args.dsn,
        include_null_user=args.include_null_user,
        include_conversation_usages=not args.no_conversation_usages,
    )
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(serialized + "\n", encoding="utf-8")
    else:
        sys.stdout.write(serialized + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
