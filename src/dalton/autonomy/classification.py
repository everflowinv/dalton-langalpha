"""Single-source automation scope classification shared by migrations, writes, and reads.

Normalization contract (identical across Python and PostgreSQL SQL; never mutates stored values):
- Leading/trailing whitespace is trimmed using the explicit ``SCOPE_TRIM_WHITESPACE`` set
  (ASCII space/tab/LF/CR/VT/FF/FS..US + Unicode NEL/NBSP/Ogham/En..Hair/Line..Paragraph/NNBSP/MMSP/Ideographic space)
  via ``str.strip(SCOPE_TRIM_WHITESPACE)`` in Python and ``BTRIM(COALESCE(expr, ''), E'...')`` in SQL.
- ``user_id``: trimmed; ``system:dalton-company`` match is case-insensitive; owner equality compares trimmed strings.
- ``name``: trimmed and compared case-sensitively for ``dalton-earnings-calendar``, ``EXCLUDED_RESEARCH_NAMES``,
  and the ``^dalton-[a-z0-9._-]+-`` prefix; covered-ticker prefix match compares ``LOWER(trimmed_name)`` against
  ``'dalton-' || LOWER(trimmed_ticker) || '-'`` after passing ``^dalton-[a-z0-9._-]+-``.
- ``metadata.dalton_kind``: extracted only when ``metadata`` is a JSON object and ``dalton_kind`` is a string,
  trimmed with ``SCOPE_TRIM_WHITESPACE``, and lowercased before checking ``RESEARCH_TRACKING_KINDS``.
- ``ticker``, ``coverage_unit.kind/status``, and ``workspace.status``: trimmed with ``SCOPE_TRIM_WHITESPACE`` and lowercased.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID

AutomationScope = Literal["personal", "research_tracking", "company"]

AUTOMATION_SCOPES: tuple[AutomationScope, ...] = (
    "personal",
    "research_tracking",
    "company",
)

SCOPE_TRIM_WHITESPACE: str = (
    " \t\n\r\x0b\x0c\x1c\x1d\x1e\x1f"
    "\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


def _build_sql_trim_chars_literal(chars: str) -> str:
    escaped: list[str] = []
    for ch in chars:
        if ch == " ":
            escaped.append(" ")
        elif ch == "\t":
            escaped.append("\\t")
        elif ch == "\n":
            escaped.append("\\n")
        elif ch == "\r":
            escaped.append("\\r")
        else:
            escaped.append(f"\\u{ord(ch):04x}")
    return "E'" + "".join(escaped) + "'"


_SQL_TRIM_CHARS: str = _build_sql_trim_chars_literal(SCOPE_TRIM_WHITESPACE)


def normalize_scope_text(value: Any) -> str:
    """Trim leading/trailing ``SCOPE_TRIM_WHITESPACE`` without altering interior characters."""
    if value is None or isinstance(value, (Mapping, list, tuple, set, frozenset, bool)):
        return ""
    return str(value).strip(SCOPE_TRIM_WHITESPACE)


def normalize_dalton_kind(metadata: Any) -> str:
    """Extract and normalize ``metadata['dalton_kind']`` identically to SQL JSONB extraction."""
    if not isinstance(metadata, Mapping):
        return ""
    raw_kind = metadata.get("dalton_kind")
    if not isinstance(raw_kind, str):
        return ""
    return raw_kind.strip(SCOPE_TRIM_WHITESPACE).lower()


def sql_trim_scope_text(expr: str) -> str:
    """Build a PostgreSQL ``BTRIM`` expression using the shared ``SCOPE_TRIM_WHITESPACE`` set."""
    return f"BTRIM(COALESCE({expr}, ''), {_SQL_TRIM_CHARS})"


def _canonical_uuid_or_none(value: Any) -> str | None:
    raw = normalize_scope_text(value)
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except (ValueError, TypeError, AttributeError):
        return None


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
    uid = normalize_scope_text(user_id)
    if not uid or uid.lower() == COMPANY_SYSTEM_USER_ID:
        return False
    cleaned_name = normalize_scope_text(name)
    if not cleaned_name or cleaned_name == "dalton-earnings-calendar":
        return False
    if normalize_dalton_kind(metadata) in RESEARCH_TRACKING_KINDS:
        return False
    if _canonical_uuid_or_none(workspace_id) is None:
        return False
    if cleaned_name in EXCLUDED_RESEARCH_NAMES:
        return False
    return bool(DALTON_TRACKING_NAME_RE.match(cleaned_name))


def _matches_active_coverage_unit(
    unit: Mapping[str, Any],
    *,
    user_id: str,
    workspace_uuid: str,
) -> bool:
    unit_ws = _canonical_uuid_or_none(unit.get("workspace_id"))
    if not unit_ws or unit_ws != workspace_uuid:
        return False
    if normalize_scope_text(unit.get("owner_user_id")) != user_id:
        return False
    if normalize_scope_text(unit.get("kind")).lower() not in ("company", "sector"):
        return False
    return normalize_scope_text(unit.get("status")).lower() == "active"


def classify_automation_scope(
    row: Mapping[str, Any],
    workspace: Mapping[str, Any] | None = None,
    *,
    coverage_unit: Mapping[str, Any] | None = None,
    coverage_units: Sequence[Mapping[str, Any]] | None = None,
    covered_tickers: Iterable[str] | None = None,
) -> AutomationScope:
    """Classify an automation row into ``personal``, ``research_tracking``, or ``company``."""
    uid = normalize_scope_text(row.get("user_id"))
    if uid.lower() == COMPANY_SYSTEM_USER_ID:
        return "company"

    name = normalize_scope_text(row.get("name"))
    if name == "dalton-earnings-calendar":
        return "research_tracking"

    if normalize_dalton_kind(row.get("metadata")) in RESEARCH_TRACKING_KINDS:
        return "research_tracking"

    ws_uuid = _canonical_uuid_or_none(row.get("workspace_id"))
    if (
        not uid
        or ws_uuid is None
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
        _matches_active_coverage_unit(u, user_id=uid, workspace_uuid=ws_uuid)
        for u in candidate_units
    ):
        return "research_tracking"

    if isinstance(workspace, Mapping):
        w_id = _canonical_uuid_or_none(workspace.get("workspace_id"))
        w_owner = normalize_scope_text(workspace.get("user_id"))
        w_status = normalize_scope_text(workspace.get("status")).lower()
        if (
            w_id
            and w_id == ws_uuid
            and w_owner == uid
            and w_status
            and w_status not in ("deleted", "unbound")
            and covered_tickers is not None
        ):
            lower_name = name.lower()
            for raw_ticker in covered_tickers:
                clean_ticker = normalize_scope_text(raw_ticker).lower()
                if clean_ticker and lower_name.startswith(f"dalton-{clean_ticker}-"):
                    return "research_tracking"

    return "personal"


def build_research_tracking_sql_predicate(alias: str = "a") -> str:
    """Build the SQL predicate matching ``classify_automation_scope == 'research_tracking'``."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError(f"Invalid SQL alias: {alias!r}")
    kinds_sql = ", ".join(f"'{k}'" for k in sorted(RESEARCH_TRACKING_KINDS))
    excluded_sql = ", ".join(f"'{n}'" for n in sorted(EXCLUDED_RESEARCH_NAMES))
    trimmed_uid = sql_trim_scope_text(f"{alias}.user_id")
    trimmed_name = sql_trim_scope_text(f"{alias}.name")
    raw_kind_expr = (
        f"CASE WHEN jsonb_typeof({alias}.metadata) = 'object' "
        f"AND jsonb_typeof({alias}.metadata->'dalton_kind') = 'string' "
        f"THEN {alias}.metadata->>'dalton_kind' ELSE '' END"
    )
    trimmed_lower_kind = f"LOWER({sql_trim_scope_text(raw_kind_expr)})"
    trimmed_unit_owner = sql_trim_scope_text("u.owner_user_id")
    trimmed_unit_kind = f"LOWER({sql_trim_scope_text('u.kind')})"
    trimmed_unit_status = f"LOWER({sql_trim_scope_text('u.status')})"
    trimmed_ws_owner = sql_trim_scope_text("w.user_id")
    trimmed_ws_status = sql_trim_scope_text("w.status")
    trimmed_cov_owner = sql_trim_scope_text("c.user_id")
    trimmed_cov_ticker = sql_trim_scope_text("c.ticker")

    return f"""(
        LOWER({trimmed_uid}) <> '{COMPANY_SYSTEM_USER_ID}'
        AND (
            {trimmed_name} = 'dalton-earnings-calendar'
            OR {trimmed_lower_kind} IN ({kinds_sql})
            OR (
                {trimmed_uid} <> ''
                AND {trimmed_name} ~ '^dalton-[a-z0-9._-]+-'
                AND {trimmed_name} NOT IN ({excluded_sql})
                AND {alias}.workspace_id IS NOT NULL
                AND (
                    EXISTS (
                        SELECT 1
                        FROM dalton_coverage_units u
                        WHERE u.workspace_id = {alias}.workspace_id
                          AND {trimmed_unit_owner} = {trimmed_uid}
                          AND {trimmed_unit_kind} IN ('company', 'sector')
                          AND {trimmed_unit_status} = 'active'
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM workspaces w
                        JOIN dalton_coverage c
                          ON {trimmed_cov_owner} = {trimmed_uid}
                         AND {trimmed_cov_ticker} <> ''
                         AND starts_with(
                             LOWER({trimmed_name}),
                             'dalton-' || LOWER({trimmed_cov_ticker}) || '-'
                         )
                        WHERE w.workspace_id = {alias}.workspace_id
                          AND {trimmed_ws_owner} = {trimmed_uid}
                          AND {trimmed_ws_status} <> ''
                          AND LOWER({trimmed_ws_status}) NOT IN ('deleted', 'unbound')
                    )
                )
            )
        )
    )"""


def build_scope_case_sql(alias: str = "a") -> str:
    """Build a SQL CASE expression identical to ``classify_automation_scope``."""
    rt_pred = build_research_tracking_sql_predicate(alias)
    trimmed_uid = sql_trim_scope_text(f"{alias}.user_id")
    return f"""CASE
        WHEN LOWER({trimmed_uid}) = '{COMPANY_SYSTEM_USER_ID}' THEN 'company'
        WHEN {rt_pred} THEN 'research_tracking'
        ELSE 'personal'
    END"""
