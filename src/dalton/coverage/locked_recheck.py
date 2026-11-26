"""Deterministic Phase 2 locked duplicate verification and shared exclusion rules.

Lives under ``src/dalton/coverage/`` so upstream ``src/server/database/workspace.py``
contains zero Dalton SQL (no references to ``dalton_coverage_units``,
``dalton_knowledge_versions``, or ``dalton_user_roles``) and delegates through a
thin ``DALTON-HOOK``.

Both lock-free Phase 1 (``src/dalton/coverage/dedup.py``) and locked Phase 2
(``check_duplicate_snapshot_unchanged`` here) share:
- ``is_reserved_workspace_row`` / ``is_dedup_excluded_workspace`` (excluding
  bound & unbound Home, Home stand-ins, and historical duplicate markers);
- ``normalize_ticker_key`` / ``canonical_workspace_ticker_key``;
- ``is_internal_team_owner`` (plus SQL-level internal-member scoping so locked
  recheck never full-scans ``workspaces``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any, Optional, Tuple

from psycopg.rows import dict_row

from src.dalton.coverage.identity import (
    canonical_workspace_ticker_key,
    is_historical_duplicate_workspace,
    normalize_ticker_key,
)
from src.server.database.home_workspace import (
    HOME_STAND_IN_KEY,
    is_home_stand_in_id,
    is_reserved_row,
)
from src.server.database.sql_fences import FENCE_LIVE_WORKSPACE
from src.server.database.workspace_names import WorkspaceNameInvalid, workspace_name_key

logger = logging.getLogger(__name__)

# Server-stamped config key carrying a normalized canonical ticker key when a
# workspace is created with an explicit ticker (committed in the same
# transaction as INSERT INTO workspaces so concurrent locked rechecks see it
# immediately even when no coverage unit row is registered).
WORKSPACE_COMPANY_TICKER_CONFIG_KEY = "dalton_company_ticker"


def row_is_home_stand_in(
    row: Mapping[str, Any],
    *,
    config_key: str = "config",
) -> bool:
    if bool(row.get("is_home_stand_in")):
        return True
    cfg = row.get(config_key)
    return isinstance(cfg, Mapping) and bool(cfg.get(HOME_STAND_IN_KEY))


def row_is_historical_duplicate(
    row: Mapping[str, Any],
    *,
    config_key: str = "config",
) -> bool:
    if bool(row.get("is_historical_duplicate")):
        return True
    cfg = row.get(config_key)
    if cfg is None:
        return False
    return is_historical_duplicate_workspace({"config": cfg})


def is_reserved_workspace_row(
    *,
    ws_id: str,
    ws_owner_id: str,
    ws_status: str,
    row: Mapping[str, Any],
    config_key: str = "config",
) -> bool:
    """Return True if the workspace is a reserved Home or Home stand-in row.

    Covers bound Home, unbound Home, pre-migration Home aliases
    (``get_home_workspace_id``), and Home stand-ins (by config flag or ID).
    """
    if row_is_home_stand_in(row, config_key=config_key):
        return True
    if ws_owner_id and ws_id and is_home_stand_in_id(ws_owner_id, ws_id):
        return True
    ws_check = {
        "workspace_id": ws_id,
        "user_id": ws_owner_id,
        "status": ws_status,
        "config": row.get(config_key),
    }
    return is_reserved_row(ws_check)


def is_dedup_excluded_workspace(
    *,
    ws_id: str,
    ws_owner_id: str,
    ws_status: str,
    row: Mapping[str, Any],
    config_key: str = "config",
) -> bool:
    """Single source of truth for workspace exclusion in Phase 1 and Phase 2 dedup.

    Excludes deleted/unbound rows, bound & unbound Home, Home stand-ins, and
    historical duplicate workspaces. Both ``dedup._collect_candidates`` and
    ``check_duplicate_snapshot_unchanged`` call this exact predicate so snapshot
    candidate sets and locked recheck sets never diverge.
    """
    if not ws_id or ws_status in ("deleted", "unbound"):
        return True
    if row_is_historical_duplicate(row, config_key=config_key):
        return True
    if is_reserved_workspace_row(
        ws_id=ws_id,
        ws_owner_id=ws_owner_id,
        ws_status=ws_status,
        row=row,
        config_key=config_key,
    ):
        return True
    return False


def _extract_row_config_ticker(row: Mapping[str, Any]) -> Optional[str]:
    direct = normalize_ticker_key(row.get("config_ticker"))
    if direct is not None:
        return direct
    cfg = row.get("config")
    if isinstance(cfg, Mapping):
        return normalize_ticker_key(cfg.get(WORKSPACE_COMPANY_TICKER_CONFIG_KEY))
    return None


async def check_duplicate_snapshot_unchanged(
    cur: Any,
    user_id: str,
    name: str,
    *,
    expected_duplicate_workspace_id: Optional[str] = None,
    expected_duplicate_workspace_name: Optional[str] = None,
    verified_company_candidates: Optional[Iterable[Tuple[Any, ...]]] = None,
    check_colleague_duplicate: bool = False,
    target_ticker: Optional[str] = None,
    conflict_out: Optional[dict[str, Any]] = None,
    conn: Any = None,
) -> bool:
    """Deterministic Phase 2 duplicate verification under ``lock_user_writes``.

    Runs entirely inside a single savepoint on the caller's transaction
    connection — never calls an LLM and never acquires a second connection from
    the pool. Any SQL error rolls back the savepoint so the outer transaction
    stays usable and fails closed by returning ``False``.

    Key invariants:
    - Uses ``is_reserved_workspace_row`` / ``is_dedup_excluded_workspace``
      (identical to Phase 1) so bound Home, stand-in, and historical duplicate
      rows never trigger false conflicts or snapshot drift.
    - Checks concrete deterministic identity matches (exact name, canonical
      ticker key, active company coverage unit ticker, latest valid company
      knowledge doc ticker, and same-tx config company ticker) BEFORE checking
      candidate-set drift, recording ``conflict_out["workspace_id"]`` ONLY for
      confirmed identity matches (never for pure drift).
    - Scopes team queries in SQL to internal team members (``dalton_user_roles``
      in local auth mode, configured PMs + ``DALTON_ALLOWED_EMAILS`` in Supabase
      auth mode) instead of full-scanning ``workspaces``.
    """
    from src.tools.secretary.approvals import is_duplicate_name

    target_ticker_keys: set[str] = set()
    name_ticker_key = canonical_workspace_ticker_key(name)
    if name_ticker_key is not None:
        target_ticker_keys.add(name_ticker_key)
    explicit_ticker_key = normalize_ticker_key(target_ticker)
    if explicit_ticker_key is not None:
        target_ticker_keys.add(explicit_ticker_key)

    try:
        target_name_key: Optional[str] = workspace_name_key(name)
    except WorkspaceNameInvalid:
        target_name_key = None

    def _same_name_or_ticker(
        target: str, candidate: str, *, candidate_is_ticker: bool = False
    ) -> bool:
        if is_duplicate_name(target, candidate):
            return True
        if target_ticker_keys:
            cand_key = (
                normalize_ticker_key(candidate)
                if candidate_is_ticker
                else canonical_workspace_ticker_key(candidate)
            )
            if cand_key is not None and cand_key in target_ticker_keys:
                return True
        return False

    def _matches_ticker_keys(ticker_key: Optional[str]) -> bool:
        return bool(
            ticker_key is not None
            and target_ticker_keys
            and ticker_key in target_ticker_keys
        )

    def _record_conflict(
        ws_id: Optional[str], *, exact_name: Optional[Tuple[str, str]] = None
    ) -> None:
        if conflict_out is None:
            return
        if ws_id:
            conflict_out["workspace_id"] = ws_id
        if exact_name is not None:
            conflict_out["exact_name_taken"] = exact_name

    has_verified_snapshot = verified_company_candidates is not None
    candidates_list = (
        list(verified_company_candidates)
        if verified_company_candidates is not None
        else []
    )
    verified_ids = {str(cand[0]) for cand in candidates_list}

    def _unpack_candidate(
        cand: Tuple[Any, ...],
    ) -> Tuple[str, str, Optional[str], bool, Optional[str]]:
        if len(cand) >= 4:
            return str(cand[0]), str(cand[1]), cand[2], True, cand[3]
        return str(cand[0]), str(cand[1]), cand[2], False, None

    async def _run_checks(check_cur: Any) -> bool:
        await check_cur.execute(
            f"""
            SELECT workspace_id::text AS workspace_id,
                   user_id,
                   name,
                   status,
                   (config->'home_stand_in') = 'true'::jsonb AS is_home_stand_in,
                   NULLIF(BTRIM(COALESCE(config->>'dalton_historical_duplicate_of', '')), '') IS NOT NULL AS is_historical_duplicate,
                   NULLIF(BTRIM(COALESCE(config->>'{WORKSPACE_COMPANY_TICKER_CONFIG_KEY}', '')), '') AS config_ticker,
                   (
                       SELECT cu.ticker
                       FROM dalton_coverage_units cu
                       WHERE cu.workspace_id = workspaces.workspace_id
                         AND cu.status = 'active'
                         AND cu.kind = 'company'
                       ORDER BY cu.updated_at DESC NULLS LAST
                       LIMIT 1
                   ) AS unit_ticker,
                   (
                       SELECT NULLIF(BTRIM(kv.content->>'ticker'), '')
                       FROM dalton_knowledge_versions kv
                       WHERE kv.workspace_id = workspaces.workspace_id
                         AND kv.schema_valid = TRUE
                         AND kv.doc_key = 'company'
                       ORDER BY kv.version_no DESC
                       LIMIT 1
                   ) AS doc_ticker
            FROM workspaces
            WHERE user_id = %(user_id)s AND {FENCE_LIVE_WORKSPACE}
            ORDER BY created_at ASC, workspace_id ASC
            """,
            {"user_id": user_id},
        )
        live_rows = await check_cur.fetchall()

        matched_expected = False
        own_eligible_by_id: dict[str, dict[str, Any]] = {}

        # Pass 1 (own workspaces): check expected duplicate and deterministic
        # identity conflicts before any drift check.
        for live_ws in live_rows:
            live_id = str(live_ws["workspace_id"])
            live_owner = str(live_ws.get("user_id") or user_id).strip()
            live_status = str(live_ws.get("status") or "").strip()
            live_name = str(live_ws.get("name") or "")

            # Never treat bound/unbound Home or Home stand-in as a conflict or drift.
            if is_reserved_workspace_row(
                ws_id=live_id,
                ws_owner_id=live_owner,
                ws_status=live_status,
                row=live_ws,
            ):
                continue

            is_excluded = is_dedup_excluded_workspace(
                ws_id=live_id,
                ws_owner_id=live_owner,
                ws_status=live_status,
                row=live_ws,
            )
            live_unit_ticker = normalize_ticker_key(live_ws.get("unit_ticker"))
            live_doc_ticker = normalize_ticker_key(live_ws.get("doc_ticker"))
            live_config_ticker = _extract_row_config_ticker(live_ws)

            if not is_excluded and live_name.strip():
                own_eligible_by_id[live_id] = {
                    "name": live_name.strip(),
                    "unit_ticker": live_unit_ticker,
                    "doc_ticker": live_doc_ticker,
                }

            if (
                expected_duplicate_workspace_id is not None
                and live_id == expected_duplicate_workspace_id
            ):
                if (
                    expected_duplicate_workspace_name is not None
                    and live_name != expected_duplicate_workspace_name
                ):
                    return False
                matches_expected = _same_name_or_ticker(name, live_name) or (
                    not is_excluded
                    and (
                        _matches_ticker_keys(live_unit_ticker)
                        or _matches_ticker_keys(live_doc_ticker)
                        or _matches_ticker_keys(live_config_ticker)
                    )
                )
                if not matches_expected:
                    return False
                matched_expected = True
                continue

            # Exact name / duplicate name check (applies even to historical
            # duplicate rows for caller's own exact name_key uniqueness).
            exact_pair: Optional[Tuple[str, str]] = None
            if target_name_key is not None:
                try:
                    if workspace_name_key(live_name) == target_name_key:
                        exact_pair = (live_name, live_id)
                except WorkspaceNameInvalid:
                    pass
            if exact_pair is not None:
                _record_conflict(live_id, exact_name=exact_pair)
                return False

            if is_excluded:
                continue

            if _same_name_or_ticker(name, live_name):
                _record_conflict(live_id)
                return False
            if (
                _matches_ticker_keys(live_unit_ticker)
                or _matches_ticker_keys(live_doc_ticker)
                or _matches_ticker_keys(live_config_ticker)
            ):
                _record_conflict(live_id)
                return False

        if expected_duplicate_workspace_id is not None and not matched_expected:
            return False

        # Pass 2 (own-only drift check when colleague check is off):
        if has_verified_snapshot and not check_colleague_duplicate:
            for live_id in own_eligible_by_id:
                if (
                    expected_duplicate_workspace_id is not None
                    and live_id == expected_duplicate_workspace_id
                ):
                    continue
                if live_id not in verified_ids:
                    return False

        if check_colleague_duplicate:
            from src.dalton import settings as dalton_settings
            from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
            from src.dalton.invites import get_configured_pm_user_ids
            from src.dalton.sharing import (
                get_dalton_allowed_emails,
                is_internal_team_owner,
            )

            internal_user_ids: Optional[set[str]] = None
            sql_params: dict[str, Any] = {}
            if dalton_settings.is_dalton_local_auth_enabled():
                await check_cur.execute(
                    """
                    SELECT user_id
                    FROM dalton_user_roles
                    WHERE tier IN ('admin', 'internal')
                    """
                )
                internal_user_ids = {
                    str(r["user_id"]).strip()
                    for r in await check_cur.fetchall()
                    if r and r.get("user_id")
                }
                scoped_ids = sorted(
                    uid for uid in internal_user_ids if uid != COMPANY_SYSTEM_USER_ID
                )
                sql_params["internal_user_ids"] = scoped_ids
                sql_params["verified_ids"] = sorted(verified_ids)
                if verified_ids:
                    owner_scope_clause = (
                        "AND (w.user_id = ANY(%(internal_user_ids)s::text[]) "
                        "OR w.workspace_id = ANY(%(verified_ids)s::uuid[]))"
                    )
                else:
                    owner_scope_clause = (
                        "AND w.user_id = ANY(%(internal_user_ids)s::text[])"
                    )
            else:
                pm_ids = sorted(
                    uid
                    for uid in get_configured_pm_user_ids()
                    if uid and uid != COMPANY_SYSTEM_USER_ID
                )
                allowed_emails = sorted(get_dalton_allowed_emails())
                sql_params["pm_user_ids"] = pm_ids
                sql_params["allowed_emails"] = allowed_emails
                sql_params["company_system_user_id"] = COMPANY_SYSTEM_USER_ID
                sql_params["verified_ids"] = sorted(verified_ids)
                internal_match_sql = (
                    "w.user_id = ANY(%(pm_user_ids)s::text[]) "
                    "OR w.user_id IN ("
                    "    SELECT u_int.user_id FROM users u_int "
                    "    WHERE LOWER(BTRIM(COALESCE(u_int.email, ''))) = ANY(%(allowed_emails)s::text[])"
                    ")"
                )
                if verified_ids:
                    owner_scope_clause = (
                        f"AND (({internal_match_sql}) OR w.workspace_id = ANY(%(verified_ids)s::uuid[])) "
                        "AND w.user_id <> %(company_system_user_id)s"
                    )
                else:
                    owner_scope_clause = (
                        f"AND ({internal_match_sql}) "
                        "AND w.user_id <> %(company_system_user_id)s"
                    )

            await check_cur.execute(
                f"""
                SELECT w.workspace_id::text AS workspace_id,
                       w.user_id,
                       w.name,
                       w.status,
                       (w.config->'home_stand_in') = 'true'::jsonb AS is_home_stand_in,
                       NULLIF(BTRIM(COALESCE(w.config->>'dalton_historical_duplicate_of', '')), '') IS NOT NULL AS is_historical_duplicate,
                       NULLIF(BTRIM(COALESCE(w.config->>'{WORKSPACE_COMPANY_TICKER_CONFIG_KEY}', '')), '') AS config_ticker,
                       u.email AS owner_email,
                       (
                           SELECT cu.ticker
                           FROM dalton_coverage_units cu
                           WHERE cu.workspace_id = w.workspace_id
                             AND cu.status = 'active'
                             AND cu.kind = 'company'
                           ORDER BY cu.updated_at DESC NULLS LAST
                           LIMIT 1
                       ) AS unit_ticker,
                       (
                           SELECT NULLIF(BTRIM(kv.content->>'ticker'), '')
                           FROM dalton_knowledge_versions kv
                           WHERE kv.workspace_id = w.workspace_id
                             AND kv.schema_valid = TRUE
                             AND kv.doc_key = 'company'
                           ORDER BY kv.version_no DESC
                           LIMIT 1
                       ) AS doc_ticker
                FROM workspaces w
                LEFT JOIN users u ON u.user_id = w.user_id
                WHERE w.status NOT IN ('deleted', 'unbound')
                  AND (w.config->'home_stand_in') IS DISTINCT FROM 'true'::jsonb
                  AND NULLIF(BTRIM(COALESCE(w.config->>'dalton_historical_duplicate_of', '')), '') IS NULL
                  {owner_scope_clause}
                ORDER BY w.created_at ASC, w.workspace_id ASC
                """,
                sql_params,
            )
            team_rows = await check_cur.fetchall()

            live_by_id: dict[str, dict[str, Any]] = {}
            eligible_team_rows: list[tuple[str, str, Optional[str], Optional[str], Optional[str]]] = []

            for row in team_rows:
                ws_id_s = str(row["workspace_id"])
                ws_owner = str(row.get("user_id") or "").strip()
                ws_status = str(row.get("status") or "").strip()
                ws_name = str(row.get("name") or "").strip()
                ws_email = row.get("owner_email")

                if not ws_name or is_dedup_excluded_workspace(
                    ws_id=ws_id_s,
                    ws_owner_id=ws_owner,
                    ws_status=ws_status,
                    row=row,
                ):
                    continue
                if not is_internal_team_owner(
                    ws_owner,
                    ws_email,
                    local_internal_user_ids=internal_user_ids,
                ):
                    continue

                unit_ticker = normalize_ticker_key(row.get("unit_ticker"))
                doc_ticker = normalize_ticker_key(row.get("doc_ticker"))
                config_ticker = _extract_row_config_ticker(row)
                live_by_id[ws_id_s] = {
                    "name": ws_name,
                    "unit_ticker": unit_ticker,
                    "doc_ticker": doc_ticker,
                }
                eligible_team_rows.append(
                    (ws_id_s, ws_name, unit_ticker, doc_ticker, config_ticker)
                )

            # Pass 1 (team workspaces): deterministic identity matches first
            # so a real duplicate is never masked by an unrelated drift.
            for ws_id_s, ws_name, unit_ticker, doc_ticker, config_ticker in eligible_team_rows:
                if (
                    expected_duplicate_workspace_id is not None
                    and ws_id_s == expected_duplicate_workspace_id
                ):
                    continue
                if _same_name_or_ticker(name, ws_name):
                    _record_conflict(ws_id_s)
                    return False
                if (
                    _matches_ticker_keys(unit_ticker)
                    or _matches_ticker_keys(doc_ticker)
                    or _matches_ticker_keys(config_ticker)
                ):
                    _record_conflict(ws_id_s)
                    return False

            # Pass 2 (team snapshot drift check): pure drift leaves conflict_out
            # empty so caller retries instead of faking company_workspace_exists.
            if has_verified_snapshot:
                for ws_id_s, _ws_name, _ut, _dt, _ct in eligible_team_rows:
                    if (
                        expected_duplicate_workspace_id is not None
                        and ws_id_s == expected_duplicate_workspace_id
                    ):
                        continue
                    if ws_id_s not in verified_ids:
                        return False

            for cand in candidates_list:
                cand_id, cand_name, cand_ticker, has_doc_ticker, cand_doc_ticker = (
                    _unpack_candidate(cand)
                )
                current = live_by_id.get(cand_id)
                if current is None:
                    return False
                if current["name"] != cand_name.strip():
                    return False
                if current["unit_ticker"] != normalize_ticker_key(cand_ticker):
                    return False
                if has_doc_ticker and current["doc_ticker"] != normalize_ticker_key(
                    cand_doc_ticker
                ):
                    return False
        elif candidates_list:
            cand_ids = [str(cand[0]) for cand in candidates_list]
            # Check if every candidate is already covered by own_eligible_by_id;
            # query by ID only when needed (e.g., cross-owner candidate tuples
            # supplied by a direct caller without check_colleague_duplicate).
            missing_ids = [cid for cid in cand_ids if cid not in own_eligible_by_id]
            current_by_id: dict[str, dict[str, Any]] = dict(own_eligible_by_id)
            if missing_ids:
                await check_cur.execute(
                    """
                    SELECT w.workspace_id::text AS workspace_id,
                           w.user_id,
                           w.name,
                           w.status,
                           (w.config->'home_stand_in') = 'true'::jsonb AS is_home_stand_in,
                           NULLIF(BTRIM(COALESCE(w.config->>'dalton_historical_duplicate_of', '')), '') IS NOT NULL AS is_historical_duplicate,
                           (
                               SELECT cu.ticker
                               FROM dalton_coverage_units cu
                               WHERE cu.workspace_id = w.workspace_id
                                 AND cu.status = 'active'
                                 AND cu.kind = 'company'
                               ORDER BY cu.updated_at DESC NULLS LAST
                               LIMIT 1
                           ) AS unit_ticker,
                           (
                               SELECT NULLIF(BTRIM(kv.content->>'ticker'), '')
                               FROM dalton_knowledge_versions kv
                               WHERE kv.workspace_id = w.workspace_id
                                 AND kv.schema_valid = TRUE
                                 AND kv.doc_key = 'company'
                               ORDER BY kv.version_no DESC
                               LIMIT 1
                           ) AS doc_ticker
                    FROM workspaces w
                    WHERE w.workspace_id = ANY(%(ws_ids)s::uuid[])
                      AND w.status NOT IN ('deleted', 'unbound')
                      AND (w.config->'home_stand_in') IS DISTINCT FROM 'true'::jsonb
                      AND NULLIF(BTRIM(COALESCE(w.config->>'dalton_historical_duplicate_of', '')), '') IS NULL
                    """,
                    {"ws_ids": missing_ids},
                )
                for r in await check_cur.fetchall():
                    ws_id_s = str(r["workspace_id"])
                    ws_owner = str(r.get("user_id") or "").strip()
                    ws_status = str(r.get("status") or "").strip()
                    ws_name = str(r.get("name") or "").strip()
                    if not ws_name or is_dedup_excluded_workspace(
                        ws_id=ws_id_s,
                        ws_owner_id=ws_owner,
                        ws_status=ws_status,
                        row=r,
                    ):
                        continue
                    current_by_id[ws_id_s] = {
                        "name": ws_name,
                        "unit_ticker": normalize_ticker_key(r.get("unit_ticker")),
                        "doc_ticker": normalize_ticker_key(r.get("doc_ticker")),
                    }
            for cand in candidates_list:
                cand_id, cand_name, cand_ticker, has_doc_ticker, cand_doc_ticker = (
                    _unpack_candidate(cand)
                )
                current = current_by_id.get(cand_id)
                if current is None:
                    return False
                if current["name"] != cand_name.strip():
                    return False
                if current["unit_ticker"] != normalize_ticker_key(cand_ticker):
                    return False
                if has_doc_ticker and current["doc_ticker"] != normalize_ticker_key(
                    cand_doc_ticker
                ):
                    return False

        return True

    try:
        if conn is not None:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as sp_cur:
                    return await _run_checks(sp_cur)
        return await _run_checks(cur)
    except Exception as exc:
        # The savepoint above rolled back cleanly on any SQL error so the outer
        # transaction stays healthy; fail closed as scope_changed / conflict.
        logger.warning(
            "Locked duplicate snapshot recheck unavailable (%s); aborting with conflict",
            type(exc).__name__,
        )
        return False
