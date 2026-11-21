#!/usr/bin/env python3
"""Read-only probe script for LLM-based company workspace deduplication.

Collects candidate workspaces (inside a ``READ ONLY`` Postgres transaction) and
runs ``_judge_candidates_with_llm(..., skip_journal=True)`` against the real
configured assist model chain without writing ``dalton_llm_journal`` rows or
creating any workspace. Note: 模型凭据刷新时应用会更新凭据记录（当默认模型的
OAuth 凭据需要刷新时，底层调用仍会更新数据库中的 ``user_oauth_tokens``）。

Usage (outside the sandbox, with DB + LLM credentials configured):
    .venv/bin/python scripts/dalton/ops/probe_workspace_dedup.py Anthropic 富途 Macro
    .venv/bin/python scripts/dalton/ops/probe_workspace_dedup.py --user-id <uid> --channel cos "Anthropic"
    .venv/bin/python scripts/dalton/ops/probe_workspace_dedup.py --show-candidates --json "NVDA"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))


def _load_dotenv_if_present() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = _REPO_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


async def probe_names(
    names: Sequence[str],
    *,
    user_id: str | None = None,
    channel: str = "web",
    show_candidates: bool = False,
) -> dict[str, Any]:
    """Run read-only candidate collection + real LLM dedup judgment for ``names``."""
    from src.dalton.coverage.dedup import (
        DEDUP_BATCH_SIZE,
        MAX_DUPLICATE_MATCHES,
        DuplicateWorkspaceCandidate,
        _collect_candidates,
        _format_candidate_line,
        _format_duplicate_message,
        _judge_candidates_with_llm,
        _refresh_live_hits,
        _resolve_caller_tier,
        _resolve_dedup_models,
        _resolve_live_hit_candidate,
    )
    from src.dalton.settings import (
        get_dalton_config,
        is_coverage_units_enabled,
        is_dalton_supabase_auth_enabled,
    )
    from src.server.database.pool import get_db_connection, get_or_create_pool

    cfg = get_dalton_config()
    eff_uid = (user_id or "").strip() or (cfg.pm_user_ids[0] if cfg.pm_user_ids else "")
    if not eff_uid:
        raise ValueError("No --user-id provided and pm_user_ids is empty")

    pool = get_or_create_pool()
    await pool.open()
    try:
        caller_tier = await _resolve_caller_tier(eff_uid)
        models = await _resolve_dedup_models()
        results: list[dict[str, Any]] = []
        candidate_lines_sample: list[str] | None = None

        for raw_name in names:
            cleaned_name = str(raw_name or "").strip()
            if not cleaned_name:
                continue
            if caller_tier == "company":
                results.append(
                    {
                        "query": cleaned_name,
                        "skipped_reason": "caller_tier=company",
                        "candidate_count": 0,
                        "matches": [],
                    }
                )
                continue

            stats = {"workspace_count": 0}
            async with get_db_connection() as conn:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute("SET TRANSACTION READ ONLY")
                    early_exit, entries = await _collect_candidates(
                        eff_uid,
                        cleaned_name,
                        caller_tier=caller_tier,
                        conn=conn,
                        stats=stats,
                    )

            if early_exit:
                results.append(
                    {
                        "query": cleaned_name,
                        "early_exit_same_name": True,
                        "candidate_count": 0,
                        "matches": [],
                    }
                )
                continue

            if show_candidates and candidate_lines_sample is None:
                candidate_lines_sample = [_format_candidate_line(e) for e in entries]

            if not entries:
                results.append(
                    {
                        "query": cleaned_name,
                        "early_exit_same_name": False,
                        "candidate_count": 0,
                        "matches": [],
                    }
                )
                continue

            hits = await _judge_candidates_with_llm(
                uid=eff_uid,
                raw_name=cleaned_name,
                entries=entries,
                stats=stats,
                is_external=(caller_tier == "external"),
                skip_journal=True,
            )

            live_hits = (await _refresh_live_hits(hits))[:MAX_DUPLICATE_MATCHES]
            match_records: list[dict[str, Any]] = []
            resolved_candidates: list[DuplicateWorkspaceCandidate] = []
            for entry, fresh_ws, conf, reason in live_hits:
                cand, _ws_id = await _resolve_live_hit_candidate(
                    eff_uid,
                    entry,
                    fresh_ws,
                    channel=channel,
                )
                resolved_candidates.append(cand)
                match_records.append(
                    {
                        "id": entry.short_id,
                        "confidence": conf,
                        "reason": reason,
                        "candidate": cand.to_dict(),
                    }
                )

            message = (
                _format_duplicate_message(cleaned_name, resolved_candidates[0])
                if resolved_candidates
                else None
            )
            results.append(
                {
                    "query": cleaned_name,
                    "early_exit_same_name": False,
                    "candidate_count": len(entries),
                    "batch_count": (len(entries) + DEDUP_BATCH_SIZE - 1) // DEDUP_BATCH_SIZE,
                    "message": message,
                    "matches": match_records,
                }
            )

        report: dict[str, Any] = {
            "user_id": eff_uid,
            "caller_tier": caller_tier,
            "channel": channel,
            "coverage_units_enabled": is_coverage_units_enabled(),
            "dalton_supabase_auth_enabled": is_dalton_supabase_auth_enabled(),
            "app_env": os.getenv("APP_ENV", ""),
            "models": models,
            "results": results,
        }
        if show_candidates:
            report["candidates"] = candidate_lines_sample or []
        return report
    finally:
        await pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    _load_dotenv_if_present()
    parser = argparse.ArgumentParser(
        description="Read-only probe for LLM-based company workspace deduplication."
    )
    parser.add_argument("names", nargs="+", help="One or more candidate workspace names to test.")
    parser.add_argument("--user-id", default=None, help="Caller user_id (defaults to first PM user).")
    parser.add_argument(
        "--channel",
        choices=("web", "cos"),
        default="web",
        help="Permission check channel (default: web).",
    )
    parser.add_argument(
        "--show-candidates",
        action="store_true",
        help="Include collected candidate lines in output.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = asyncio.run(
        probe_names(
            args.names,
            user_id=args.user_id,
            channel=args.channel,
            show_candidates=args.show_candidates,
        )
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(
        f"user_id={report['user_id']} tier={report['caller_tier']} "
        f"channel={report['channel']} models={report['models']}"
    )
    print(
        f"flags: coverage_units={report['coverage_units_enabled']} "
        f"supabase_auth={report['dalton_supabase_auth_enabled']}"
    )
    if args.show_candidates:
        cands = report.get("candidates") or []
        print(f"\n--- Candidates ({len(cands)}) ---")
        for line in cands:
            print(line)
    for item in report["results"]:
        print(f"\n=== Query: {item['query']} ===")
        if item.get("skipped_reason"):
            print(f"Skipped: {item['skipped_reason']}")
            continue
        if item.get("early_exit_same_name"):
            print("Early exit: caller already owns a workspace with the same name (WorkspaceNameTaken).")
            continue
        print(
            f"Candidates: {item['candidate_count']} "
            f"(batches: {item.get('batch_count', 0)})"
        )
        if not item["matches"]:
            print("Matches: none")
            continue
        print(f"Message: {item['message']}")
        for m in item["matches"]:
            print(
                f"  - {m['id']} [{m['confidence']}] {m['reason']} -> "
                f"{json.dumps(m['candidate'], ensure_ascii=False)}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
