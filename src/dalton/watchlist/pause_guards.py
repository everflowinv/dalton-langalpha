"""Dalton watchlist pause-reason persistence, row-locked resume guards, and earnings-stage validation."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from src.dalton.coverage.models import VALID_EARNINGS_STAGES

_EARN_STAGE_RE = re.compile(
    r"^dalton-(?P<slug>[a-z0-9][a-z0-9._-]{0,19})-earn-.+-(?P<stage>tm3|t0|tp1)(?:\.json)?$",
    re.IGNORECASE,
)


def is_dalton_managed_automation(row: Mapping[str, Any] | None) -> bool:
    """Return True iff ``row`` represents a Dalton-managed automation."""
    if not isinstance(row, Mapping):
        return False
    name = str(row.get("name") or "").strip().lower()
    if name.startswith("dalton-"):
        return True
    meta = row.get("metadata")
    if isinstance(meta, Mapping):
        if meta.get("dalton_managed") or meta.get("ticker"):
            return True
    return False


def apply_manual_pause_metadata(
    row: Mapping[str, Any] | None,
    effective_kwargs: dict[str, Any],
) -> None:
    """Persist ``metadata.paused_by_user = True`` when a user manually pauses a Dalton task."""
    if not is_dalton_managed_automation(row):
        return
    base_meta: dict[str, Any] = {}
    if isinstance(row, Mapping) and isinstance(row.get("metadata"), Mapping):
        base_meta.update(dict(row["metadata"]))
    if isinstance(effective_kwargs.get("metadata"), Mapping):
        base_meta.update(dict(effective_kwargs["metadata"]))
    base_meta["paused_by_user"] = True
    effective_kwargs["metadata"] = base_meta


def can_override_user_pause_locked(
    locked_row: Mapping[str, Any],
    *,
    override_user_pause: bool,
    override_pause_versions: Mapping[str, int] | None,
) -> bool:
    """Return True iff ``override_user_pause`` is authorized for ``locked_row``'s live ``config_version``.

    Missing ``override_pause_versions`` ALWAYS refuses overriding a user pause (fail-closed).
    """
    if not override_user_pause or override_pause_versions is None:
        return False
    auto_id = str(locked_row.get("automation_id") or "").strip()
    if not auto_id or auto_id not in override_pause_versions:
        return False
    live_ver = int(locked_row.get("config_version") or 1)
    return live_ver == int(override_pause_versions[auto_id])


def build_locked_resume_reconciler(
    *,
    override_user_pause: bool = False,
    override_pause_versions: Mapping[str, int] | None = None,
    skip_policy_pause: bool = False,
) -> Callable[[Any, dict[str, Any], Any], Awaitable[None]]:
    """Build a ``locked_row_reconciler`` that verifies pause markers & ``config_version`` inside FOR UPDATE."""

    async def _reconcile_locked(
        locked_row: Any,
        effective_kwargs: dict[str, Any],
        _locked_conn: Any,
    ) -> None:
        if not isinstance(locked_row, Mapping):
            return
        locked_status = str(locked_row.get("status") or "").strip().lower()
        locked_meta = (
            dict(locked_row.get("metadata") or {})
            if isinstance(locked_row.get("metadata"), Mapping)
            else {}
        )
        # Never transition disabled back to active
        if locked_status == "disabled":
            effective_kwargs["status"] = "disabled"
            effective_kwargs["next_run_at"] = None
            return

        # Never auto-resume replaced_by_tracker or handoff-paused rows
        if locked_meta.get("replaced_by_tracker") is True or bool(
            str(locked_meta.get("handoff_paused_reason") or "").strip()
        ):
            effective_kwargs["status"] = "paused"
            effective_kwargs["next_run_at"] = None
            merged = dict(locked_meta)
            if isinstance(effective_kwargs.get("metadata"), Mapping):
                merged.update(dict(effective_kwargs["metadata"]))
            for key in ("replaced_by_tracker", "handoff_paused_reason", "paused_by_user", "paused_by_policy"):
                if key in locked_meta:
                    merged[key] = locked_meta[key]
            effective_kwargs["metadata"] = merged
            return

        # Respect policy pause if requested by caller (e.g. non-standard resume loop)
        if skip_policy_pause and locked_meta.get("paused_by_policy"):
            effective_kwargs["status"] = "paused"
            effective_kwargs["next_run_at"] = None
            merged = dict(locked_meta)
            if isinstance(effective_kwargs.get("metadata"), Mapping):
                merged.update(dict(effective_kwargs["metadata"]))
            merged["paused_by_policy"] = True
            effective_kwargs["metadata"] = merged
            return

        # Check user pause with row-locked config_version verification
        if locked_meta.get("paused_by_user"):
            allowed = can_override_user_pause_locked(
                locked_row,
                override_user_pause=override_user_pause,
                override_pause_versions=override_pause_versions,
            )
            if not allowed:
                effective_kwargs["status"] = "paused"
                effective_kwargs["next_run_at"] = None
                merged = dict(locked_meta)
                if isinstance(effective_kwargs.get("metadata"), Mapping):
                    merged.update(dict(effective_kwargs["metadata"]))
                merged["paused_by_user"] = True
                effective_kwargs["metadata"] = merged
            else:
                merged = dict(locked_meta)
                if isinstance(effective_kwargs.get("metadata"), Mapping):
                    merged.update(dict(effective_kwargs["metadata"]))
                merged.pop("paused_by_user", None)
                effective_kwargs["metadata"] = merged

    return _reconcile_locked


def parse_earnings_stage_task_name(name: str | None) -> tuple[str, str] | None:
    """Return ``(ticker_slug, stage)`` for ``dalton-<slug>-earn-*-<tm3|t0|tp1>``, else ``None``."""
    clean = str(name or "").strip().lower()
    m = _EARN_STAGE_RE.match(clean)
    if not m:
        return None
    stage = m.group("stage").lower()
    if stage not in VALID_EARNINGS_STAGES:
        return None
    return m.group("slug").lower(), stage


async def validate_earnings_stage_allowed_for_user(
    user_id: str,
    task_name: str,
    *,
    ticker: str | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Validate an earnings-stage task against the user's live watchlist tier & policy.

    Returns an error dict ``{"ok": False, "error": "earnings_stage_disabled", "message": ...}``
    when the stage is disabled by the user's live coverage policy, or ``None`` when allowed.
    """
    parsed = parse_earnings_stage_task_name(task_name)
    if parsed is None:
        return None
    slug, stage = parsed
    uid = str(user_id or "").strip()
    if not uid:
        return None

    from src.dalton.database import coverage as coverage_db
    from src.dalton.watchlist.policies import (
        get_user_tier_policy,
        resolve_effective_company_policy,
    )

    candidates: list[str] = []
    if ticker and str(ticker).strip():
        candidates.append(str(ticker).strip().upper())
    slug_upper = slug.upper()
    if slug_upper not in candidates:
        candidates.append(slug_upper)
    if "-" in slug_upper:
        dot_form = slug_upper.replace("-", ".")
        if dot_form not in candidates:
            candidates.append(dot_form)

    cov_row: dict[str, Any] | None = None
    try:
        for cand in candidates:
            cov_row = await coverage_db.get_coverage_row(uid, cand, conn=conn)
            if cov_row is not None:
                break
    except Exception as exc:
        msg = str(exc).lower()
        if any(
            tok in msg
            for tok in ("pool is not open", "not initialized", "connection pool", "does not exist")
        ):
            return None
        raise

    if cov_row is None:
        return None

    sym = str(cov_row.get("ticker") or candidates[0]).strip().upper()
    if bool(cov_row.get("is_private")):
        return {
            "ok": False,
            "error": "earnings_stage_disabled",
            "message": f"{sym} 为未上市公司，不启用财报 {stage} 阶段自动任务。",
        }

    live_tier = str(cov_row.get("tier") or "").strip().lower()
    base_policy = await get_user_tier_policy(uid, live_tier, conn=conn)
    eff_policy = resolve_effective_company_policy(base_policy, cov_row.get("task_overrides"))
    if stage not in set(eff_policy.earnings_stages):
        return {
            "ok": False,
            "error": "earnings_stage_disabled",
            "message": (
                f"{sym} 当前分档（{live_tier or 'unknown'}）未启用财报 {stage} 阶段自动任务"
                f"（允许阶段：{list(eff_policy.earnings_stages)}）。"
            ),
        }
    return None


__all__ = [
    "apply_manual_pause_metadata",
    "build_locked_resume_reconciler",
    "can_override_user_pause_locked",
    "is_dalton_managed_automation",
    "parse_earnings_stage_task_name",
    "validate_earnings_stage_allowed_for_user",
]
