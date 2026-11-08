"""Per-task core success/failure evaluators and registry for OC-1 data tasks (§3.4.3).

PR 0-1 implements the registry framework plus the T5 (`cn-leverage-daily`) and
T18 (`megacap-relative-signal-monitor`) evaluators to lock the interface contract.
"""

from __future__ import annotations

from collections.abc import Callable
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from src.dalton.data_jobs.runner_wrapper import (
    DEFAULT_WORKSPACE_DIR,
    EXIT_CODE_NO_OUTPUT_TIMEOUT,
    EXIT_CODE_TOTAL_TIMEOUT,
    extract_last_json_object,
)

TerminalRunStatus = Literal["succeeded", "failed", "timed_out"]
TimeoutKind = Literal["total", "no_output"]

T1_JOB_KEY = "bbg-data-pull-asia"
T5_JOB_KEY = "cn-leverage-daily"
T18_JOB_KEY = "megacap-relative-signal-monitor"
T29_JOB_KEY = "bbg-data-pull-us"


def previous_weekday(ref_date: date) -> date:
    """Return the most recent weekday (Monday–Friday) strictly before ``ref_date`` (§3.4.3)."""
    cursor = ref_date - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor

T5_UPDATE_TOP_KEYS: tuple[str, ...] = (
    "core",
    "futures",
    "etf",
    "limit_up",
    "aux",
    "quant_unwind",
)
T5_AUX_SOFT_KEYS: tuple[str, ...] = (
    "northbound",
    "microcap",
    "convertible_bonds",
)
T5_PIPELINE_CORE_STEPS: tuple[str, ...] = (
    "两融核心",
    "股指期货基差",
    "宽基ETF份额",
    "量化中性拥挤/Unwind",
)
T5_PIPELINE_SOFT_STEPS: tuple[str, ...] = (
    "北向资金",
    "微盘股相对强弱",
    "可转债估值",
)


@dataclass
class TaskEvaluationContext:
    """Inputs supplied to a per-task core evaluator (§3.4.1, §3.4.3)."""

    job_key: str
    exit_code: int | None
    oom_killed: bool = False
    timed_out: bool = False
    timeout_kind: TimeoutKind | None = None
    stdout: str = ""
    data_root: Path = field(default_factory=lambda: Path("/app/var/dalton/openclaw-data"))
    started_at: datetime | None = None
    reference_date: date | None = None


@dataclass
class TaskEvaluationResult:
    """Outcome produced by a per-task core evaluator (§3.4.1, §3.4.3)."""

    ok: bool
    status: TerminalRunStatus
    failure_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


EvaluatorFn = Callable[[TaskEvaluationContext], TaskEvaluationResult]

EVALUATOR_REGISTRY: dict[str, EvaluatorFn] = {}
_TASK_CODE_ALIASES: dict[str, str] = {
    "T1": T1_JOB_KEY,
    "T5": T5_JOB_KEY,
    "T18": T18_JOB_KEY,
    "T29": T29_JOB_KEY,
}


def register_evaluator(*keys: str) -> Callable[[EvaluatorFn], EvaluatorFn]:
    """Register a core evaluator function for one or more ``job_key`` / ``task_code`` identifiers."""

    def _decorator(fn: EvaluatorFn) -> EvaluatorFn:
        for key in keys:
            normalized = str(key).strip()
            if normalized:
                EVALUATOR_REGISTRY[normalized] = fn
        return fn

    return _decorator


def get_evaluator(job_key_or_code: str) -> EvaluatorFn | None:
    """Look up the registered evaluator for ``job_key`` (or task code like ``T5``)."""
    key = str(job_key_or_code or "").strip()
    if key in EVALUATOR_REGISTRY:
        return EVALUATOR_REGISTRY[key]
    canonical = _TASK_CODE_ALIASES.get(key.upper())
    if canonical and canonical in EVALUATOR_REGISTRY:
        return EVALUATOR_REGISTRY[canonical]
    return None


def extract_all_json_objects(text: str) -> list[dict[str, Any]]:
    """Extract all top-level JSON dictionary objects from ``text`` in appearance order."""
    if not text:
        return []
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    idx = 0
    length = len(text)
    while idx < length:
        brace_pos = text.find("{", idx)
        if brace_pos == -1:
            break
        try:
            obj, end_pos = decoder.raw_decode(text, brace_pos)
        except json.JSONDecodeError:
            idx = brace_pos + 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        idx = max(brace_pos + 1, end_pos)
    return found


def resolve_data_root_path(raw_path: str | Path, data_root: Path) -> Path:
    """Map a container workspace path (`/Users/everflow/.openclaw/workspace/...`) into ``data_root``."""
    raw_str = str(raw_path).strip()
    ws_prefix = DEFAULT_WORKSPACE_DIR.rstrip("/") + "/"
    if raw_str.startswith(ws_prefix):
        rel = raw_str[len(ws_prefix) :]
        return data_root / rel
    candidate = Path(raw_str)
    if not candidate.is_absolute():
        return data_root / candidate
    if candidate.exists():
        return candidate
    return candidate


@register_evaluator(T5_JOB_KEY, "T5")
def evaluate_t5_cn_leverage_daily(ctx: TaskEvaluationContext) -> TaskEvaluationResult:
    """Core evaluator for T5 (`cn-leverage-daily`, §3.4.3).

    Rules:
    1. In `update_all()` output, only fail if any of the 6 top-level keys
       (`core`, `futures`, `etf`, `limit_up`, `aux`, `quant_unwind`) is itself a dict
       containing `"error"` (`isinstance(v, dict) and "error" in v`).
       Errors inside `aux["northbound"]`, `aux["microcap"]`, or `aux["convertible_bonds"]`
       record warnings only and do NOT fail.
    2. In `<data_root>/state/cn-leverage/pipeline_status.json`, the 4 core steps
       (`两融核心`, `股指期货基差`, `宽基ETF份额`, `量化中性拥挤/Unwind`) must have
       `status == "completed"`; `北向资金`, `微盘股相对强弱`, `可转债估值` not completed
       record warnings only and do NOT fail.
    3. `<data_root>/state/cn-leverage/quant_unwind_signals.csv` must be non-empty and its
       latest `date` must be a valid recent trading date.
    4. `<data_root>/research-outputs/cn-leverage/index.html` must exist, be non-empty, and
       have been updated in this run (when `started_at` is provided).
    """
    warnings: list[str] = []
    objects = extract_all_json_objects(ctx.stdout)

    update_dict: dict[str, Any] | None = None
    for obj in objects:
        if obj.get("ok") is False and "error" in obj:
            return TaskEvaluationResult(
                ok=False,
                status="failed",
                failure_reason=f"t5_cli_error:{obj.get('error')}",
                warnings=warnings,
            )
        candidate = obj.get("result") if isinstance(obj.get("result"), dict) else obj
        if isinstance(candidate, dict) and any(k in candidate for k in T5_UPDATE_TOP_KEYS):
            update_dict = candidate
            break

    if update_dict is None:
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason="t5_missing_update_all_output",
            warnings=warnings,
        )

    for top_key in T5_UPDATE_TOP_KEYS:
        if top_key not in update_dict:
            return TaskEvaluationResult(
                ok=False,
                status="failed",
                failure_reason=f"t5_missing_top_key:{top_key}",
                warnings=warnings,
            )
        val = update_dict[top_key]
        if isinstance(val, dict) and "error" in val:
            return TaskEvaluationResult(
                ok=False,
                status="failed",
                failure_reason=f"t5_top_step_error:{top_key}:{val['error']}",
                warnings=warnings,
            )

    aux_val = update_dict.get("aux")
    if isinstance(aux_val, dict):
        for soft_key in T5_AUX_SOFT_KEYS:
            sub_val = aux_val.get(soft_key)
            if isinstance(sub_val, dict) and "error" in sub_val:
                warnings.append(f"aux.{soft_key}:{sub_val['error']}")

    pipeline_path = ctx.data_root / "state" / "cn-leverage" / "pipeline_status.json"
    if not pipeline_path.is_file():
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason="t5_missing_pipeline_status_json",
            warnings=warnings,
        )
    try:
        pipeline_status = json.loads(pipeline_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason=f"t5_invalid_pipeline_status_json:{exc}",
            warnings=warnings,
        )
    if not isinstance(pipeline_status, dict):
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason="t5_pipeline_status_not_dict",
            warnings=warnings,
        )

    for step_name in T5_PIPELINE_CORE_STEPS:
        step_info = pipeline_status.get(step_name)
        step_st = step_info.get("status") if isinstance(step_info, dict) else None
        if step_st != "completed":
            return TaskEvaluationResult(
                ok=False,
                status="failed",
                failure_reason=f"t5_core_step_incomplete:{step_name}:{step_st}",
                warnings=warnings,
            )

    for soft_step in T5_PIPELINE_SOFT_STEPS:
        step_info = pipeline_status.get(soft_step)
        step_st = step_info.get("status") if isinstance(step_info, dict) else None
        if step_st != "completed":
            warnings.append(f"pipeline_status.{soft_step}:{step_st or 'missing'}")

    unwind_csv_path = ctx.data_root / "state" / "cn-leverage" / "quant_unwind_signals.csv"
    if not unwind_csv_path.is_file():
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason="t5_missing_quant_unwind_signals_csv",
            warnings=warnings,
        )
    try:
        with open(unwind_csv_path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            dates = [
                str(row.get("date") or "").strip()
                for row in reader
                if str(row.get("date") or "").strip()
            ]
    except Exception as exc:
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason=f"t5_invalid_quant_unwind_signals_csv:{exc}",
            warnings=warnings,
        )
    if not dates:
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason="t5_empty_quant_unwind_signals_csv",
            warnings=warnings,
        )
    latest_date_str = max(dates)
    try:
        latest_date = date.fromisoformat(latest_date_str[:10])
    except ValueError:
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason=f"t5_invalid_unwind_date:{latest_date_str}",
            warnings=warnings,
        )
    ref_date = ctx.reference_date
    if ref_date is None:
        base_dt = ctx.started_at or datetime.now(timezone.utc)
        if base_dt.tzinfo is None:
            base_dt = base_dt.replace(tzinfo=timezone.utc)
        ref_date = base_dt.astimezone(ZoneInfo("Asia/Shanghai")).date()
    min_valid_date = previous_weekday(ref_date)
    if latest_date < min_valid_date or latest_date > ref_date:
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason=(
                f"t5_stale_unwind_date:{latest_date_str}:"
                f"ref={ref_date.isoformat()}:min={min_valid_date.isoformat()}"
            ),
            warnings=warnings,
        )

    dashboard_path = ctx.data_root / "research-outputs" / "cn-leverage" / "index.html"
    if not dashboard_path.is_file() or dashboard_path.stat().st_size == 0:
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason="t5_missing_or_empty_dashboard_html",
            warnings=warnings,
        )
    if ctx.started_at is not None:
        started_ts = ctx.started_at.timestamp()
        if dashboard_path.stat().st_mtime < started_ts - 1.0:
            return TaskEvaluationResult(
                ok=False,
                status="failed",
                failure_reason="t5_dashboard_html_not_updated_in_run",
                warnings=warnings,
            )

    return TaskEvaluationResult(
        ok=True,
        status="succeeded",
        warnings=warnings,
        details={"latest_unwind_date": latest_date_str},
    )


@register_evaluator(T18_JOB_KEY, "T18")
def evaluate_t18_megacap_relative_signal_monitor(
    ctx: TaskEvaluationContext,
) -> TaskEvaluationResult:
    """Core evaluator for T18 (`megacap-relative-signal-monitor`, §3.4.3).

    Parses ``stdout`` as a whole text to extract the last complete JSON object
    (supporting both single-line and multi-line ``indent=2`` JSON), verifies
    ``"ok": true``, and checks that the output chart PNG exists on disk.
    """
    payload = extract_last_json_object(ctx.stdout)
    if not isinstance(payload, dict):
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason="t18_stdout_missing_json_object",
        )
    if payload.get("ok") is not True:
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason=f"t18_json_ok_false:{payload.get('error') or payload.get('ok')}",
        )

    chart_raw = payload.get("chart") or payload.get("chart_path")
    if not isinstance(chart_raw, str) or not chart_raw.strip():
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason="t18_missing_chart_field",
        )

    chart_path = resolve_data_root_path(chart_raw, ctx.data_root)
    if not chart_path.is_file() or chart_path.stat().st_size == 0:
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason=f"t18_chart_file_missing:{chart_raw}",
        )

    return TaskEvaluationResult(
        ok=True,
        status="succeeded",
        details={
            "date": payload.get("date"),
            "chart": str(chart_path),
        },
    )


@register_evaluator(T1_JOB_KEY, "T1", T29_JOB_KEY, "T29")
def evaluate_t1_t29_bbg_data_pull(
    ctx: TaskEvaluationContext,
) -> TaskEvaluationResult:
    """Core evaluator for T1 (`bbg-data-pull-asia`) and T29 (`bbg-data-pull-us`, §3.4.3).

    Checks that the last JSON object in ``stdout`` has ``ok == true`` and ``lines >= 252``.
    """
    payload = extract_last_json_object(ctx.stdout)
    if not isinstance(payload, dict):
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason="bbg_stdout_missing_json_object",
        )
    if payload.get("ok") is not True:
        reason = payload.get("reason") or payload.get("error")
        if reason == "openclaw_not_updated":
            return TaskEvaluationResult(
                ok=False,
                status="failed",
                failure_reason="openclaw_not_updated",
            )
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason=f"bbg_json_ok_false:{reason or payload.get('ok')}",
        )

    lines_val = payload.get("lines")
    if isinstance(lines_val, bool) or not isinstance(lines_val, int) or lines_val < 252:
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason=f"bbg_insufficient_lines:{lines_val}",
        )

    return TaskEvaluationResult(
        ok=True,
        status="succeeded",
        details={
            "lines": lines_val,
            "source": payload.get("source"),
            "path": payload.get("path"),
            "timestamp": payload.get("timestamp"),
        },
    )


def evaluate_task_run(ctx: TaskEvaluationContext) -> TaskEvaluationResult:
    """Run pre-checks (`timed_out`, wrapper exit codes 124/125, `oom_killed`, `exit_code == 0`) and dispatch."""
    if ctx.timed_out or ctx.exit_code in (EXIT_CODE_TOTAL_TIMEOUT, EXIT_CODE_NO_OUTPUT_TIMEOUT):
        kind: TimeoutKind = ctx.timeout_kind or (
            "no_output" if ctx.exit_code == EXIT_CODE_NO_OUTPUT_TIMEOUT else "total"
        )
        reason = "no_output_timed_out" if kind == "no_output" else "total_timed_out"
        return TaskEvaluationResult(
            ok=False,
            status="timed_out",
            failure_reason=reason,
            details={"timeout_kind": kind},
        )
    if ctx.oom_killed:
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason="oom_killed",
        )
    if ctx.exit_code != 0:
        if ctx.job_key in (T1_JOB_KEY, T29_JOB_KEY, "T1", "T29"):
            last_obj = extract_last_json_object(ctx.stdout)
            if isinstance(last_obj, dict):
                err_reason = last_obj.get("reason") or last_obj.get("error")
                if isinstance(err_reason, str) and err_reason.strip():
                    return TaskEvaluationResult(
                        ok=False,
                        status="failed",
                        failure_reason=err_reason.strip(),
                    )
            if "openclaw_not_updated" in (ctx.stdout or ""):
                return TaskEvaluationResult(
                    ok=False,
                    status="failed",
                    failure_reason="openclaw_not_updated",
                )
        return TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason=f"exit_code_{ctx.exit_code}",
        )

    evaluator = get_evaluator(ctx.job_key)
    if evaluator is None:
        return TaskEvaluationResult(ok=True, status="succeeded")
    return evaluator(ctx)
