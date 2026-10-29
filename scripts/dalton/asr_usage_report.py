#!/usr/bin/env python3
"""Read-only usage summary and bridge/backend reconciliation report for Seed-ASR transcription jobs (Backlog A1d).

Reconciles bridge-side JSONL (`asr_usage.jsonl` or `var/dalton/asr_jobs/*/state.json`)
against backend `dalton_transcription_jobs` records by `(job_id, attempt)` without
double counting. Attributes every attempt to the verified A1b job creator
(`created_by_user_id`), and reports duration in seconds without displaying a fake
`0` cost when Seed-ASR unit pricing is unconfigured.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import sys
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from asr_jobs import build_attempt_usage_record, read_bridge_usage_jsonl  # noqa: E402
from src.dalton.database.transcriptions import (  # noqa: E402
    reconcile_transcription_usage,
    summarize_transcription_usage,
)

DEFAULT_JOBS_DIR = REPO_ROOT / "var" / "dalton" / "asr_jobs"
DEFAULT_USAGE_JSONL = DEFAULT_JOBS_DIR / "asr_usage.jsonl"


def load_bridge_usage_records(
    *,
    usage_jsonl_path: Path | None = None,
    jobs_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Load and deduplicate bridge-side attempt usage records by `(job_id, attempt)`."""
    by_key: dict[tuple[str, int], dict[str, Any]] = {}

    eff_jsonl = usage_jsonl_path
    if eff_jsonl is None:
        env_file = os.environ.get("DALTON_ASR_USAGE_FILE", "").strip()
        if env_file:
            eff_jsonl = Path(env_file)
        elif jobs_dir is not None:
            eff_jsonl = Path(jobs_dir) / "asr_usage.jsonl"
        else:
            eff_jsonl = DEFAULT_USAGE_JSONL

    if eff_jsonl is not None and eff_jsonl.is_file():
        for rec in read_bridge_usage_jsonl(eff_jsonl):
            jid = str(rec.get("job_id") or "").strip()
            att = int(rec.get("attempt") or 0)
            if jid and att >= 1:
                by_key[(jid, att)] = rec

    eff_jobs_dir = Path(jobs_dir) if jobs_dir is not None else None
    if eff_jobs_dir is not None and eff_jobs_dir.is_dir():
        for entry in sorted(eff_jobs_dir.iterdir()):
            if not entry.is_dir():
                continue
            state_file = entry / "state.json"
            if not state_file.is_file():
                continue
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(state, dict):
                continue
            raw_usage = state.get("usage") if isinstance(state.get("usage"), dict) else {}
            raw_attempts = raw_usage.get("attempts")
            if isinstance(raw_attempts, list):
                for item in raw_attempts:
                    if not isinstance(item, dict):
                        continue
                    jid = str(item.get("job_id") or state.get("job_id") or "").strip()
                    att = int(item.get("attempt") or 0)
                    st = str(item.get("status") or "").strip()
                    if jid and att >= 1 and st in ("succeeded", "failed"):
                        if (jid, att) not in by_key:
                            by_key[(jid, att)] = dict(item)
            st_str = str(state.get("status") or "").strip()
            if st_str in ("succeeded", "failed"):
                built = build_attempt_usage_record(state, status=st_str)
                key = (built["job_id"], int(built["attempt"]))
                if key[0] and key[1] >= 1 and key not in by_key:
                    by_key[key] = built

    return [by_key[k] for k in sorted(by_key)]


def load_backend_job_records(backend_json_path: Path | None) -> list[dict[str, Any]]:
    """Load backend `dalton_transcription_jobs` rows from a JSON or JSONL file."""
    if backend_json_path is None or not backend_json_path.is_file():
        return []
    raw_text = backend_json_path.read_text(encoding="utf-8").strip()
    if not raw_text:
        return []
    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, list):
            return [r for r in parsed if isinstance(r, dict)]
        if isinstance(parsed, dict):
            if isinstance(parsed.get("jobs"), list):
                return [r for r in parsed["jobs"] if isinstance(r, dict)]
            return [parsed]
    except json.JSONDecodeError:
        pass

    rows: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            item = json.loads(s)
            if isinstance(item, dict):
                rows.append(item)
        except Exception:
            continue
    return rows


def format_usage_report_text(summary: Mapping[str, Any]) -> str:
    """Format a human-readable usage and reconciliation report without fabricating a `$0` cost."""
    lines: list[str] = []
    totals = summary.get("totals") if isinstance(summary.get("totals"), Mapping) else {}
    recon = (
        summary.get("reconciliation")
        if isinstance(summary.get("reconciliation"), Mapping)
        else {}
    )
    lines.append("=== Dalton Seed-ASR Usage & Reconciliation Report ===")
    lines.append(
        f"Total Attempts (deduplicated by job_id+attempt): {totals.get('attempt_count', 0)} "
        f"(succeeded={totals.get('succeeded_attempts', 0)}, failed={totals.get('failed_attempts', 0)}, "
        f"jobs={totals.get('job_count', 0)})"
    )
    lines.append(
        f"Total Duration: {totals.get('duration_seconds', 0.0):.2f}s | "
        f"Submitted: {totals.get('submitted_seconds', 0.0):.2f}s | "
        f"Completed: {totals.get('completed_seconds', 0.0):.2f}s | "
        f"Segments: {totals.get('completed_segments', 0)}/{totals.get('segment_count', 0)}"
    )
    lines.append(f"Pricing Note: {totals.get('cost_note', '单价未配置，仅按秒统计时长')}")
    lines.append(
        f"Reconciliation: matched={recon.get('matched_count', 0)}, "
        f"backend_only={recon.get('backend_only_count', 0)}, "
        f"bridge_only={recon.get('bridge_only_count', 0)}, "
        f"mismatches={recon.get('mismatch_count', 0)}"
    )

    by_user = summary.get("by_user")
    if isinstance(by_user, Sequence) and by_user:
        lines.append("\n--- By User ---")
        for row in by_user:
            if not isinstance(row, Mapping):
                continue
            lines.append(
                f"  user={row.get('user_id')} | attempts={row.get('attempt_count')} "
                f"(ok={row.get('succeeded_attempts')}, fail={row.get('failed_attempts')}) | "
                f"duration={float(row.get('duration_seconds') or 0.0):.2f}s | "
                f"segments={row.get('segment_count')}"
            )

    by_date = summary.get("by_date")
    if isinstance(by_date, Sequence) and by_date:
        lines.append("\n--- By Date ---")
        for row in by_date:
            if not isinstance(row, Mapping):
                continue
            lines.append(
                f"  date={row.get('date')} | attempts={row.get('attempt_count')} "
                f"(ok={row.get('succeeded_attempts')}, fail={row.get('failed_attempts')}) | "
                f"duration={float(row.get('duration_seconds') or 0.0):.2f}s | "
                f"segments={row.get('segment_count')}"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize Seed-ASR transcription usage and reconcile bridge JSONL against backend records."
    )
    parser.add_argument(
        "--bridge-jsonl",
        type=Path,
        default=None,
        help="Path to bridge asr_usage.jsonl file.",
    )
    parser.add_argument(
        "--jobs-dir",
        type=Path,
        default=None,
        help="Path to bridge var/dalton/asr_jobs directory.",
    )
    parser.add_argument(
        "--backend-json",
        type=Path,
        default=None,
        help="Path to backend dalton_transcription_jobs JSON/JSONL export.",
    )
    parser.add_argument("--user", default=None, help="Filter by creator user_id.")
    parser.add_argument("--start-date", default=None, help="Filter start date (YYYY-MM-DD).")
    parser.add_argument("--end-date", default=None, help="Filter end date (YYYY-MM-DD).")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    bridge_records = load_bridge_usage_records(
        usage_jsonl_path=args.bridge_jsonl,
        jobs_dir=args.jobs_dir,
    )
    backend_jobs = load_backend_job_records(args.backend_json)
    summary = summarize_transcription_usage(
        backend_jobs,
        bridge_records=bridge_records,
        user_id=args.user,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(format_usage_report_text(summary))
    return 0


__all__ = [
    "DEFAULT_JOBS_DIR",
    "DEFAULT_USAGE_JSONL",
    "format_usage_report_text",
    "load_backend_job_records",
    "load_bridge_usage_records",
    "main",
    "reconcile_transcription_usage",
    "summarize_transcription_usage",
]


if __name__ == "__main__":
    raise SystemExit(main())
