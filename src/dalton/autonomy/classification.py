"""Single-source automation scope classification shared by migrations, writes, and reads."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID, is_company_system_user

AutomationScope = Literal["personal", "research_tracking", "company"]

AUTOMATION_SCOPES: tuple[AutomationScope, ...] = (
    "personal",
    "research_tracking",
    "company",
)

RESEARCH_TRACKING_KINDS: frozenset[str] = frozenset(
    {
        "company_watch",
        "weekly_delta",
        "price_move",
        "earnings_tm3",
        "earnings_t0",
        "earnings_tp1",
        "sector_watch",
        "sector_delta",
        "calendar",
    }
)

EXCLUDED_RESEARCH_NAMES: frozenset[str] = frozenset(
    {
        "dalton-director-daily",
        "dalton-director-weekly",
        "dalton-data-health",
        "dalton-earnings-calendar",
    }
)

DALTON_TRACKING_NAME_RE = re.compile(r"^dalton-[a-z0-9._-]+-")


def needs_workspace_tracking_lookup(
    user_id: str | None,
    *,
    name: str | None,
    workspace_id: Any,
    metadata: Mapping[str, Any] | None,
) -> bool:
    """Return True only when DB workspace/coverage state can change the classification."""
    if is_company_system_user(user_id):
        return False
    cleaned_name = str(name or "").strip()
    if not cleaned_name or cleaned_name == "dalton-earnings-calendar":
        return False
    if isinstance(metadata, Mapping):
        kind = str(metadata.get("dalton_kind") or "").strip()
        if kind in RESEARCH_TRACKING_KINDS:
            return False
    ws_id = str(workspace_id or "").strip()
    if not ws_id:
        return False
    if cleaned_name in EXCLUDED_RESEARCH_NAMES:
        return False
    return bool(DALTON_TRACKING_NAME_RE.match(cleaned_name))


def _matches_active_coverage_unit(
    unit: Mapping[str, Any],
    *,
    user_id: str,
    workspace_id: str,
) -> bool:
    unit_ws = str(unit.get("workspace_id") or "").strip().lower()
    if not unit_ws or unit_ws != workspace_id.lower():
        return False
    if str(unit.get("owner_user_id") or "").strip() != user_id:
        return False
    if str(unit.get("kind") or "").strip() not in ("company", "sector"):
        return False
    return str(unit.get("status") or "").strip() == "active"


def classify_automation_scope(
    row: Mapping[str, Any],
    workspace: Mapping[str, Any] | None = None,
    *,
    coverage_unit: Mapping[str, Any] | None = None,
    coverage_units: Sequence[Mapping[str, Any]] | None = None,
    covered_tickers: Iterable[str] | None = None,
) -> AutomationScope:
    """Classify an automation row into ``personal``, ``research_tracking``, or ``company``."""
    uid = str(row.get("user_id") or "").strip()
    if is_company_system_user(uid):
        return "company"

    name = str(row.get("name") or "").strip()
    if name == "dalton-earnings-calendar":
        return "research_tracking"

    metadata = row.get("metadata")
    if isinstance(metadata, Mapping):
        kind = str(metadata.get("dalton_kind") or "").strip()
        if kind in RESEARCH_TRACKING_KINDS:
            return "research_tracking"

    ws_id = str(row.get("workspace_id") or "").strip()
    if (
        not uid
        or not ws_id
        or not name
        or name in EXCLUDED_RESEARCH_NAMES
        or not DALTON_TRACKING_NAME_RE.match(name)
    ):
        return "personal"

    candidate_units: list[Mapping[str, Any]] = []
    if isinstance(coverage_unit, Mapping):
        candidate_units.append(coverage_unit)
    if coverage_units:
        candidate_units.extend(u for u in coverage_units if isinstance(u, Mapping))
    if any(
        _matches_active_coverage_unit(u, user_id=uid, workspace_id=ws_id)
        for u in candidate_units
    ):
        return "research_tracking"

    if isinstance(workspace, Mapping):
        w_id = str(workspace.get("workspace_id") or "").strip().lower()
        w_owner = str(workspace.get("user_id") or "").strip()
        w_status = str(workspace.get("status") or "").strip()
        if (
            w_id
            and w_id == ws_id.lower()
            and w_owner == uid
            and w_status
            and w_status not in ("deleted", "unbound")
            and covered_tickers is not None
        ):
            lower_name = name.lower()
            for raw_ticker in covered_tickers:
                clean_ticker = str(raw_ticker or "").strip().lower()
                if clean_ticker and lower_name.startswith(f"dalton-{clean_ticker}-"):
                    return "research_tracking"

    return "personal"


def build_research_tracking_sql_predicate(alias: str = "a") -> str:
    """Build the SQL predicate matching ``classify_automation_scope == 'research_tracking'``."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError(f"Invalid SQL alias: {alias!r}")
    kinds_sql = ", ".join(f"'{k}'" for k in sorted(RESEARCH_TRACKING_KINDS))
    excluded_sql = ", ".join(f"'{n}'" for n in sorted(EXCLUDED_RESEARCH_NAMES))
    return f"""(
        {alias}.user_id <> '{COMPANY_SYSTEM_USER_ID}'
        AND (
            {alias}.name = 'dalton-earnings-calendar'
            OR COALESCE({alias}.metadata->>'dalton_kind', '') IN ({kinds_sql})
            OR (
                {alias}.name ~ '^dalton-[a-z0-9._-]+-'
                AND {alias}.name NOT IN ({excluded_sql})
                AND {alias}.workspace_id IS NOT NULL
                AND (
                    EXISTS (
                        SELECT 1
                        FROM dalton_coverage_units u
                        WHERE u.workspace_id = {alias}.workspace_id
                          AND u.owner_user_id = {alias}.user_id
                          AND u.kind IN ('company', 'sector')
                          AND u.status = 'active'
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM workspaces w
                        JOIN dalton_coverage c
                          ON c.user_id = {alias}.user_id
                         AND NULLIF(BTRIM(c.ticker), '') IS NOT NULL
                         AND starts_with(LOWER({alias}.name), 'dalton-' || LOWER(BTRIM(c.ticker)) || '-')
                        WHERE w.workspace_id = {alias}.workspace_id
                          AND w.user_id = {alias}.user_id
                          AND w.status NOT IN ('deleted', 'unbound')
                    )
                )
            )
        )
    )"""


def build_scope_case_sql(alias: str = "a") -> str:
    """Build a SQL CASE expression identical to ``classify_automation_scope``."""
    rt_pred = build_research_tracking_sql_predicate(alias)
    return f"""CASE
        WHEN {alias}.user_id = '{COMPANY_SYSTEM_USER_ID}' THEN 'company'
        WHEN {rt_pred} THEN 'research_tracking'
        ELSE 'personal'
    END"""
