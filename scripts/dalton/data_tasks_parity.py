"""Read-only shadow-run parity comparison tool for OC-1 data tasks (§6.1 item 1).

Compares ``<data_root>/.dalton-freeze/<job_key>/<scheduled_slot_utc>/done.json``
between Dalton's data root and OpenClaw's data root. Only slots where both sides
have written ``done.json`` are compared for content parity; slots where only one
side has ``done.json`` are reported as ``missing_one_side``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

FREEZE_DIR_NAME = ".dalton-freeze"
DONE_FILENAME = "done.json"


def _extract_file_sha256_map(done_data: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    raw_sha_map = done_data.get("file_sha256")
    if isinstance(raw_sha_map, Mapping):
        for rel, val in raw_sha_map.items():
            if isinstance(val, str):
                out[str(rel)] = val
    raw_files = done_data.get("files")
    if isinstance(raw_files, Mapping):
        for rel, meta in raw_files.items():
            if isinstance(meta, Mapping) and isinstance(meta.get("sha256"), str):
                out[str(rel)] = str(meta["sha256"])
            elif isinstance(meta, str):
                out[str(rel)] = meta
    return out


def _extract_csv_summary_map(done_data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    raw_csv = done_data.get("csv_summary")
    if isinstance(raw_csv, Mapping):
        for rel, meta in raw_csv.items():
            if isinstance(meta, Mapping):
                out[str(rel)] = {
                    "row_count": meta.get("row_count"),
                    "latest_date": meta.get("latest_date"),
                }
    raw_files = done_data.get("files")
    if isinstance(raw_files, Mapping):
        for rel, meta in raw_files.items():
            rel_str = str(rel)
            if (
                isinstance(meta, Mapping)
                and (
                    rel_str.lower().endswith(".csv")
                    or "row_count" in meta
                    or "latest_date" in meta
                )
                and rel_str not in out
            ):
                out[rel_str] = {
                    "row_count": meta.get("row_count"),
                    "latest_date": meta.get("latest_date"),
                }
    return out


def _diff_core_metrics(
    dalton_metrics: Any,
    openclaw_metrics: Any,
    prefix: str = "",
) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    if isinstance(dalton_metrics, Mapping) and isinstance(openclaw_metrics, Mapping):
        all_keys = sorted(
            {str(k) for k in dalton_metrics.keys()}
            | {str(k) for k in openclaw_metrics.keys()}
        )
        for key in all_keys:
            metric_path = f"{prefix}.{key}" if prefix else key
            if key not in dalton_metrics:
                diffs.append(
                    {
                        "category": "core_metrics",
                        "field": f"core_metrics.{metric_path}",
                        "metric": metric_path,
                        "dalton": None,
                        "openclaw": openclaw_metrics[key],
                    }
                )
            elif key not in openclaw_metrics:
                diffs.append(
                    {
                        "category": "core_metrics",
                        "field": f"core_metrics.{metric_path}",
                        "metric": metric_path,
                        "dalton": dalton_metrics[key],
                        "openclaw": None,
                    }
                )
            else:
                diffs.extend(
                    _diff_core_metrics(
                        dalton_metrics[key],
                        openclaw_metrics[key],
                        prefix=metric_path,
                    )
                )
        return diffs

    if dalton_metrics != openclaw_metrics:
        metric_name = prefix or "root"
        diffs.append(
            {
                "category": "core_metrics",
                "field": f"core_metrics.{metric_name}",
                "metric": metric_name,
                "dalton": dalton_metrics,
                "openclaw": openclaw_metrics,
            }
        )
    return diffs


def _compare_done_records(
    dalton_done: Mapping[str, Any],
    openclaw_done: Mapping[str, Any],
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []

    d_exit = dalton_done.get("exit_code")
    o_exit = openclaw_done.get("exit_code")
    if d_exit != o_exit:
        differences.append(
            {
                "category": "exit_code",
                "field": "exit_code",
                "dalton": d_exit,
                "openclaw": o_exit,
            }
        )

    d_sha = _extract_file_sha256_map(dalton_done)
    o_sha = _extract_file_sha256_map(openclaw_done)
    for rel in sorted(set(d_sha.keys()) | set(o_sha.keys())):
        if rel not in d_sha or rel not in o_sha:
            differences.append(
                {
                    "category": "file_missing",
                    "field": f"files.{rel}",
                    "file": rel,
                    "dalton": d_sha.get(rel),
                    "openclaw": o_sha.get(rel),
                }
            )
        elif d_sha[rel] != o_sha[rel]:
            differences.append(
                {
                    "category": "sha256",
                    "field": f"files.{rel}.sha256",
                    "file": rel,
                    "dalton": d_sha[rel],
                    "openclaw": o_sha[rel],
                }
            )

    d_csv = _extract_csv_summary_map(dalton_done)
    o_csv = _extract_csv_summary_map(openclaw_done)
    for rel in sorted(set(d_csv.keys()) | set(o_csv.keys())):
        d_meta = d_csv.get(rel) or {}
        o_meta = o_csv.get(rel) or {}
        if d_meta.get("row_count") != o_meta.get("row_count"):
            differences.append(
                {
                    "category": "csv_row_count",
                    "field": f"csv.{rel}.row_count",
                    "file": rel,
                    "dalton": d_meta.get("row_count"),
                    "openclaw": o_meta.get("row_count"),
                }
            )
        if d_meta.get("latest_date") != o_meta.get("latest_date"):
            differences.append(
                {
                    "category": "csv_latest_date",
                    "field": f"csv.{rel}.latest_date",
                    "file": rel,
                    "dalton": d_meta.get("latest_date"),
                    "openclaw": o_meta.get("latest_date"),
                }
            )

    d_metrics = dalton_done.get("core_metrics") or {}
    o_metrics = openclaw_done.get("core_metrics") or {}
    differences.extend(_diff_core_metrics(d_metrics, o_metrics))

    return differences


def compare_data_tasks_parity(
    dalton_data_root: Path | str,
    openclaw_data_root: Path | str,
    *,
    job_key: str | None = None,
) -> dict[str, Any]:
    """Compare frozen per-slot ``done.json`` records between ``dalton_data_root`` and ``openclaw_data_root``.

    Strictly read-only on both directory trees.
    """
    dalton_root = Path(dalton_data_root)
    openclaw_root = Path(openclaw_data_root)
    dalton_freeze = dalton_root / FREEZE_DIR_NAME
    openclaw_freeze = openclaw_root / FREEZE_DIR_NAME
    cleaned_job_key = str(job_key).strip() if job_key is not None and str(job_key).strip() else None

    if cleaned_job_key is not None:
        job_keys = [cleaned_job_key]
    else:
        discovered_jobs: set[str] = set()
        if dalton_freeze.is_dir():
            for child in dalton_freeze.iterdir():
                if child.is_dir():
                    discovered_jobs.add(child.name)
        if openclaw_freeze.is_dir():
            for child in openclaw_freeze.iterdir():
                if child.is_dir():
                    discovered_jobs.add(child.name)
        job_keys = sorted(discovered_jobs)

    all_slots: list[dict[str, Any]] = []
    jobs_report: dict[str, dict[str, Any]] = {}
    consistent_count = 0
    inconsistent_count = 0
    missing_one_side_count = 0

    for jk in job_keys:
        d_job_dir = dalton_freeze / jk
        o_job_dir = openclaw_freeze / jk
        slot_names: set[str] = set()
        if d_job_dir.is_dir():
            for child in d_job_dir.iterdir():
                if child.is_dir():
                    slot_names.add(child.name)
        if o_job_dir.is_dir():
            for child in o_job_dir.iterdir():
                if child.is_dir():
                    slot_names.add(child.name)

        job_slots_map: dict[str, dict[str, Any]] = {}
        for slot_name in sorted(slot_names):
            d_done_path = d_job_dir / slot_name / DONE_FILENAME
            o_done_path = o_job_dir / slot_name / DONE_FILENAME
            has_dalton = d_done_path.is_file()
            has_openclaw = o_done_path.is_file()

            if not has_dalton and not has_openclaw:
                continue

            if not (has_dalton and has_openclaw):
                missing_side = "openclaw" if has_dalton else "dalton"
                missing_one_side_count += 1
                slot_entry: dict[str, Any] = {
                    "job_key": jk,
                    "scheduled_slot_utc": slot_name,
                    "status": "missing_one_side",
                    "consistent": False,
                    "compared": False,
                    "missing_side": missing_side,
                    "differences": [
                        {
                            "category": "missing_done_json",
                            "field": DONE_FILENAME,
                            "missing_side": missing_side,
                            "dalton_done": has_dalton,
                            "openclaw_done": has_openclaw,
                        }
                    ],
                }
                all_slots.append(slot_entry)
                job_slots_map[slot_name] = slot_entry
                continue

            try:
                d_done = json.loads(d_done_path.read_text(encoding="utf-8"))
                o_done = json.loads(o_done_path.read_text(encoding="utf-8"))
            except Exception as exc:
                inconsistent_count += 1
                slot_entry = {
                    "job_key": jk,
                    "scheduled_slot_utc": slot_name,
                    "status": "inconsistent",
                    "consistent": False,
                    "compared": True,
                    "missing_side": None,
                    "differences": [
                        {
                            "category": "invalid_done_json",
                            "field": DONE_FILENAME,
                            "error": str(exc),
                        }
                    ],
                }
                all_slots.append(slot_entry)
                job_slots_map[slot_name] = slot_entry
                continue

            diffs = _compare_done_records(d_done, o_done)
            if not diffs:
                consistent_count += 1
                slot_entry = {
                    "job_key": jk,
                    "scheduled_slot_utc": slot_name,
                    "status": "consistent",
                    "consistent": True,
                    "compared": True,
                    "missing_side": None,
                    "differences": [],
                }
            else:
                inconsistent_count += 1
                slot_entry = {
                    "job_key": jk,
                    "scheduled_slot_utc": slot_name,
                    "status": "inconsistent",
                    "consistent": False,
                    "compared": True,
                    "missing_side": None,
                    "differences": diffs,
                }
            all_slots.append(slot_entry)
            job_slots_map[slot_name] = slot_entry

        jobs_report[jk] = {"slots": job_slots_map}

    compared_count = consistent_count + inconsistent_count
    reason: str | None = None
    if not dalton_root.is_dir() or not openclaw_root.is_dir():
        reason = "directory_not_found"
    elif (
        cleaned_job_key is not None
        and (dalton_freeze.is_dir() or openclaw_freeze.is_dir())
        and not (dalton_freeze / cleaned_job_key).is_dir()
        and not (openclaw_freeze / cleaned_job_key).is_dir()
    ):
        reason = "job_key_not_matched"
    elif compared_count == 0:
        reason = "no_completed_slots"

    summary: dict[str, Any] = {
        "total_slots": len(all_slots),
        "consistent_count": consistent_count,
        "inconsistent_count": inconsistent_count,
        "missing_one_side_count": missing_one_side_count,
        "all_consistent": (
            reason is None
            and compared_count > 0
            and inconsistent_count == 0
            and missing_one_side_count == 0
        ),
    }
    if reason is not None:
        summary["reason"] = reason

    return {
        "dalton_data_root": str(dalton_root),
        "openclaw_data_root": str(openclaw_root),
        "job_key_filter": job_key,
        "summary": summary,
        "slots": all_slots,
        "jobs": jobs_report,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare frozen OC-1 data task artifacts between Dalton and OpenClaw data roots."
    )
    parser.add_argument(
        "dalton_data_root",
        nargs="?",
        help="Path to Dalton data root directory.",
    )
    parser.add_argument(
        "openclaw_data_root",
        nargs="?",
        help="Path to OpenClaw data root directory.",
    )
    parser.add_argument(
        "--dalton-root",
        "--dalton-data-root",
        dest="dalton_root_flag",
        help="Path to Dalton data root directory (alternative to positional arg).",
    )
    parser.add_argument(
        "--openclaw-root",
        "--openclaw-data-root",
        dest="openclaw_root_flag",
        help="Path to OpenClaw data root directory (alternative to positional arg).",
    )
    parser.add_argument(
        "--job-key",
        "-j",
        dest="job_key",
        default=None,
        help="Optional job_key filter.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    dalton_root = args.dalton_root_flag or args.dalton_data_root
    openclaw_root = args.openclaw_root_flag or args.openclaw_data_root
    if not dalton_root or not openclaw_root:
        parser.error("Both dalton_data_root and openclaw_data_root are required.")
    report = compare_data_tasks_parity(
        dalton_root,
        openclaw_root,
        job_key=args.job_key,
    )
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    summary = report["summary"]
    if summary["all_consistent"]:
        return 0
    if summary.get("reason") is not None or (
        summary["consistent_count"] + summary["inconsistent_count"] == 0
    ):
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
