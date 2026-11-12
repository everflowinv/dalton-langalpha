"""
Database functions for automations: creating, reading, updating and
deleting the rows, and the scheduler's claims on them.

Run history lives in ``automation_executions``. The execution columns and
status filters stay here because every automation read carries its newest run.
"""

import logging
import re
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.pool import get_db_connection
from src.server.utils.db import UpdateQueryBuilder

logger = logging.getLogger(__name__)


# =============================================================================
# Automation CRUD
# =============================================================================


# DALTON-HOOK(company-automations): include scope, notify storage, versioning, actor, and soft-delete columns
AUTOMATION_COLUMNS = """
    automation_id, user_id, name, file_name, description,
    trigger_type, cron_expression, timezone, trigger_config,
    next_run_at, last_run_at,
    instruction, workspace_id, llm_model, additional_context,
    thread_strategy, conversation_thread_id,
    status, max_failures, failure_count, disable_reason,
    delivery_config, metadata,
    scope, notify_enabled, notify_routes, notify_storage_mode,
    config_version, active_version, created_by, updated_by, deleted_at, deleted_by,
    created_at, updated_at
"""

# One execution as every reader returns it: the history, the run feed and an
# automation's newest run. ``e`` is automation_executions.
EXECUTION_COLUMNS = """
    e.automation_execution_id, e.automation_id,
    e.status, e.conversation_thread_id,
    e.scheduled_at, e.started_at, e.completed_at,
    e.error_message, e.skip_reason, e.failure_reason, e.server_id,
    e.delivery_result, e.created_at, e.dismissed_at,
    e.result_excerpt AS excerpt
"""

UNSETTLED_STATUSES = "('pending', 'waiting', 'running')"


def not_a_server_skip(alias: str) -> str:
    """A firing that stands for its automation's last run: anything but one
    the server skipped because the thread stayed busy or it stopped."""
    return (
        f"({alias}.status <> 'skipped' OR {alias}.skip_reason IS NULL"
        f" OR {alias}.skip_reason NOT IN ('thread_busy', 'interrupted', 'precheck'))"  # DALTON-HOOK(precheck-hidden)
    )


def _reached_execution_probe(outer: str, alias: str, predicate: str) -> str:
    """One last-run probe limited to the token computer before LIMIT.

    The effective workspace is the run thread's workspace, or ``outer``'s
    current workspace when the run has no thread. Home and any other computer
    drop out of this probe, so a later probe cannot fall back to them.
    """
    # DALTON-HOOK(company-automations): computer tokens read each last-run probe on that computer only
    effective = f"COALESCE(reach_thread.workspace_id, {outer}.workspace_id)"
    narrowed = f" AND {predicate}" if predicate else ""
    return (
        f"(SELECT {alias}.automation_execution_id\n"
        f"                     FROM automation_executions {alias}\n"
        f"                     LEFT JOIN conversation_threads reach_thread\n"
        f"                       ON reach_thread.conversation_thread_id = {alias}.conversation_thread_id\n"
        f"                     WHERE {alias}.automation_id = {outer}.automation_id"
        f"{narrowed}\n"
        f"                       AND {effective} <> %s::uuid\n"
        f"                       AND EXISTS (SELECT 1 FROM workspaces execution_workspace\n"
        f"                            WHERE execution_workspace.workspace_id = {effective}\n"
        f"                              AND execution_workspace.computer_id = %s::uuid)\n"
        f"                     ORDER BY {alias}.created_at DESC, {alias}.automation_execution_id DESC\n"
        f"                     LIMIT 1)"
    )


def _last_execution_computer_params(
    user_id: str, computer_id: str
) -> tuple[str, str, str, str, str, str]:
    """Binds for the three probes, in unsettled, non-skip, newest order."""
    home_id = get_home_workspace_id(user_id)
    return (home_id, computer_id, home_id, computer_id, home_id, computer_id)


def _last_execution_join(outer: str, *, computer_id: Optional[str] = None) -> str:
    """A LEFT JOIN LATERAL giving the automation row ``outer`` its
    ``last_execution``: the newest firing still in flight, else the newest
    that is not a skip the user never chose, else the newest.

    A newer skipped firing must not hide a run that is still going, or the
    automation reads idle and offers to run again. Nor may a skip the server
    made (its thread stayed busy, or it stopped) hide the failed run before
    it, which is what asks the user for attention. COALESCE runs each probe
    only when the ones before it find nothing, and each is one index descent.
    DALTON-HOOK(company-automations): limit last-run selection to the token computer.
    When every probe is empty, the id comparison matches nothing and
    ``last_execution`` is null.

    ``computer_id`` only switches on computer-token reach. The value is not
    written into SQL. Set, each probe applies Home exclusion and that
    computer to the run's effective workspace before LIMIT, and the caller
    binds ``_last_execution_computer_params``. Omitted, the SQL is the
    user-wide selection and has no extra placeholders.
    """
    if computer_id is None:
        return f"""
        LEFT JOIN LATERAL (
            SELECT to_jsonb(le_row) AS last_execution
            FROM (
                SELECT {EXECUTION_COLUMNS}
                FROM automation_executions e
                WHERE e.automation_execution_id = COALESCE(
                    (SELECT u.automation_execution_id
                     FROM automation_executions u
                     WHERE u.automation_id = {outer}.automation_id
                       AND u.status IN {UNSETTLED_STATUSES}
                     ORDER BY u.created_at DESC, u.automation_execution_id DESC
                     LIMIT 1),
                    (SELECT c.automation_execution_id
                     FROM automation_executions c
                     WHERE c.automation_id = {outer}.automation_id
                       AND {not_a_server_skip("c")}
                     ORDER BY c.created_at DESC, c.automation_execution_id DESC
                     LIMIT 1),
                    (SELECT n.automation_execution_id
                     FROM automation_executions n
                     WHERE n.automation_id = {outer}.automation_id
                     ORDER BY n.created_at DESC, n.automation_execution_id DESC
                     LIMIT 1)
                )
            ) le_row
        ) le ON TRUE
    """
    # DALTON-HOOK(company-automations): same precedence, with computer reach inside each probe
    unsettled = _reached_execution_probe(
        outer, "u", f"u.status IN {UNSETTLED_STATUSES}"
    )
    eligible = _reached_execution_probe(outer, "c", not_a_server_skip("c"))
    newest = _reached_execution_probe(outer, "n", "")
    return f"""
        LEFT JOIN LATERAL (
            SELECT to_jsonb(le_row) AS last_execution
            FROM (
                SELECT {EXECUTION_COLUMNS}
                FROM automation_executions e
                WHERE e.automation_execution_id = COALESCE(
                    {unsettled},
                    {eligible},
                    {newest}
                )
            ) le_row
        ) le ON TRUE
    """


# How many times a create picks a file name again after another create took
# the one it picked.
_FILE_NAME_ATTEMPTS = 5


def file_name_stem(name: str) -> str:
    """The file name an automation called ``name`` gets when nobody picks
    one, before ``.json`` and any ``-2`` that keeps it apart from another of
    the user's. Migration 058 named existing rows by the same rule, in SQL."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return stem[:48].strip("-") or "automation"


async def _free_file_name(user_id: str, name: str, conn) -> str:
    stem = file_name_stem(name)
    async with conn.cursor() as cur:
        # DALTON-HOOK(company-automations): match partial unique index (deleted_at IS NULL)
        await cur.execute(
            """
            SELECT file_name FROM automations
            WHERE user_id = %s AND deleted_at IS NULL AND (file_name = %s OR file_name LIKE %s)
            """,
            (user_id, f"{stem}.json", f"{stem}-%.json"),
        )
        taken = {row[0] for row in await cur.fetchall()}
    candidate, n = f"{stem}.json", 1
    while candidate in taken:
        n += 1
        candidate = f"{stem}-{n}.json"
    return candidate


async def create_automation(
    user_id: str,
    name: str,
    trigger_type: str,
    instruction: str,
    *,
    file_name: Optional[str] = None,
    description: Optional[str] = None,
    cron_expression: Optional[str] = None,
    timezone: str = "UTC",
    trigger_config: Optional[Dict[str, Any]] = None,
    next_run_at: Optional[datetime] = None,
    workspace_id: Optional[str] = None,
    llm_model: Optional[str] = None,
    additional_context: Optional[List[Dict[str, Any]]] = None,
    thread_strategy: str = "new",
    conversation_thread_id: Optional[str] = None,
    max_failures: int = 3,
    delivery_config: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    status: str = "active",  # DALTON-HOOK(coverage-tracking)
    scope: Optional[str] = None,  # DALTON-HOOK(company-automations)
    created_by: Optional[str] = None,
    conn=None,
) -> Dict[str, Any]:
    """Create a new automation.

    Without a ``file_name`` it gets the first free one derived from ``name``.
    A create elsewhere can take that name between the read and the insert,
    so a clash reads the names again; a ``file_name`` the caller picked is
    its own, and a clash on it raises ``UniqueViolation``.
    """
    # DALTON-HOOK(company-automations): enforce non-company ownership, canonical scope, and v1 audit_history on create
    import contextlib
    from datetime import timezone as _dt_tz
    from unittest.mock import NonCallableMock
    from src.dalton.company_identity import is_company_system_user
    from src.dalton.database.company_automations import (
        _is_real_db_conn,
        _to_iso_str,
        build_create_history_columns,
        lock_and_fetch_user_preferences_for_snapshot,
        resolve_automation_scope_for_write,
    )
    from src.dalton.services.company_automations import (
        UNSET,
        get_current_file_coalesce_state,
        get_current_history_context,
    )

    if is_company_system_user(user_id) or str(scope or "").strip().lower() == "company":
        raise ValueError("Company automations cannot be created via personal create_automation")

    automation_id = str(uuid4())
    hist_ctx = get_current_history_context()
    if created_by is not None:
        effective_created_by: Optional[str] = created_by
    elif hist_ctx is not None and not isinstance(hist_ctx.actor_user_id, type(UNSET)):
        effective_created_by = hist_ctx.actor_user_id
    else:
        effective_created_by = user_id

    coalesce_state = get_current_file_coalesce_state()
    eff_status = status or "active"
    if coalesce_state is not None and coalesce_state.mode == "create_pause":
        initial_desired_status = "paused"
    else:
        initial_desired_status = "paused" if eff_status == "paused" else "active"

    effective_metadata: Dict[str, Any] = dict(metadata or {})
    if trigger_type == "once" and next_run_at is not None:
        once_iso = _to_iso_str(next_run_at)
        if once_iso is not None:
            effective_metadata["once_schedule_at"] = once_iso

    now_iso = datetime.now(_dt_tz.utc).isoformat()

    clashes = 0
    async with get_db_connection(conn) as conn:
        is_real_conn = _is_real_db_conn(conn)
        outer_tx = conn.transaction() if is_real_conn else contextlib.nullcontext()
        # DALTON-HOOK(company-automations): lock user_preferences, build v1 snapshot, and insert in one transaction
        async with outer_tx:
            if (
                coalesce_state is not None
                and coalesce_state.prelocked_pref_row is not None
            ):
                pref_row = coalesce_state.prelocked_pref_row
            elif hist_ctx is not None and hist_ctx.prelocked_pref_row is not None:
                pref_row = hist_ctx.prelocked_pref_row
            else:
                pref_row = await lock_and_fetch_user_preferences_for_snapshot(
                    user_id,
                    conn=conn,
                    notify_storage_mode="legacy",
                )
            effective_scope = (
                scope
                if scope in ("personal", "research_tracking")
                else await resolve_automation_scope_for_write(
                    user_id,
                    name=name,
                    workspace_id=workspace_id,
                    metadata=effective_metadata,
                    conn=conn,
                )
            )
            # DALTON-HOOK(company-automations): build initial v1 configuration snapshot and history
            hist_cols = await build_create_history_columns(
                {
                    "automation_id": automation_id,
                    "user_id": user_id,
                    "name": name,
                    "description": description,
                    "status": eff_status,
                    "trigger_type": trigger_type,
                    "cron_expression": cron_expression,
                    "next_run_at": next_run_at,
                    "trigger_config": trigger_config or {},
                    "timezone": timezone,
                    "instruction": instruction,
                    "workspace_id": workspace_id,
                    "llm_model": llm_model,
                    "additional_context": additional_context,
                    "thread_strategy": thread_strategy,
                    "conversation_thread_id": conversation_thread_id,
                    "max_failures": max_failures,
                    "delivery_config": delivery_config or {},
                    "metadata": effective_metadata,
                    "scope": effective_scope,
                    "notify_storage_mode": "legacy",
                },
                actor_user_id=effective_created_by,
                desired_status=initial_desired_status,
                pref_row=pref_row,
                changed_at_iso=now_iso,
                conn=conn if not isinstance(conn, NonCallableMock) else None,
            )
            while True:
                chosen = file_name or await _free_file_name(user_id, name, conn)
                try:
                    # A savepoint inside a caller's transaction, so a clash
                    # leaves it usable for the next attempt.
                    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(f"""
                            INSERT INTO automations (
                                automation_id, user_id, name, file_name, description,
                                trigger_type, cron_expression, timezone, trigger_config,
                                next_run_at,
                                instruction, workspace_id, llm_model, additional_context,
                                thread_strategy, conversation_thread_id,
                                status, max_failures, failure_count,
                                delivery_config, metadata,
                                scope, created_by, updated_by,
                                config_version, active_version, audit_history,
                                created_at, updated_at
                            )
                            VALUES (
                                %s, %s, %s, %s, %s,
                                %s, %s, %s, %s,
                                %s,
                                %s, %s, %s, %s,
                                %s, %s,
                                %s, %s, 0,
                                %s, %s,
                                %s, %s, %s,
                                %s, %s, %s,
                                NOW(), NOW()
                            )
                            RETURNING {AUTOMATION_COLUMNS}
                        """, (
                            automation_id, user_id, name, chosen, description,
                            trigger_type, cron_expression, timezone,
                            Json(trigger_config or {}),
                            next_run_at,
                            instruction, workspace_id, llm_model,
                            Json(additional_context) if additional_context else None,
                            thread_strategy, conversation_thread_id,
                            eff_status,  # DALTON-HOOK(coverage-tracking)
                            max_failures,
                            Json(delivery_config or {}),
                            Json(effective_metadata),
                            effective_scope,  # DALTON-HOOK(company-automations)
                            effective_created_by,
                            hist_cols["updated_by"],
                            hist_cols["config_version"],
                            hist_cols["active_version"],
                            Json(hist_cols["audit_history"]),
                        ))
                        result = await cur.fetchone()
                except UniqueViolation:
                    clashes += 1
                    if file_name is not None or clashes == _FILE_NAME_ATTEMPTS:
                        raise
                    continue
                logger.info(
                    f"[automation_db] create_automation automation_id={automation_id} "
                    f"user_id={user_id} trigger_type={trigger_type}"
                )
                return dict(result)


# Whether the pinned thread is one a run of this automation created, which is
# how a persistent thread is told from a pinned conversation once the first
# run has pinned it (``models.automation.in_own_thread``).
_OWNS_THREAD = """
    EXISTS (
        SELECT 1 FROM conversation_threads t
        WHERE t.conversation_thread_id = automations.conversation_thread_id
          AND t.metadata->'origin'->>'type' = 'automation'
          AND t.metadata->'origin'->>'id' = automations.automation_id::text
    ) AS owns_thread
"""


async def get_automation(
    automation_id: str,
    user_id: str,
    *,
    computer_id: Optional[str] = None,
    conn=None,
) -> Optional[Dict[str, Any]]:
    """Get a single automation by ID, verifying ownership, with its newest
    run and ``owns_thread``.

    ``computer_id`` limits ``last_execution`` to a run on that computer,
    outside Home. Omitted, the newest run stays the user-wide one.
    """
    # DALTON-HOOK(company-automations): optional computer reach for last_execution; omitted stays user-wide
    reach_params: tuple[str, ...] = ()
    if computer_id is not None:
        reach_params = _last_execution_computer_params(user_id, computer_id)
    async with get_db_connection(conn) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # DALTON-HOOK(company-automations): hide soft-deleted rows and order deterministically
            await cur.execute(f"""
                SELECT {AUTOMATION_COLUMNS}, le.last_execution, {_OWNS_THREAD}
                FROM automations
                {_last_execution_join("automations", computer_id=computer_id)}
                WHERE automation_id = %s AND user_id = %s AND deleted_at IS NULL
            """, (*reach_params, automation_id, user_id))

            result = await cur.fetchone()
            return dict(result) if result else None


async def get_automation_including_deleted(
    automation_id: str,
    user_id: str,
    *,
    computer_id: Optional[str] = None,
    conn=None,
) -> Optional[Dict[str, Any]]:
    """Same row as ``get_automation``, including one already in the recycle bin.

    Settlement and the abandoned-firing sweep use this so a running execution
    of a deleted task still finishes. ``get_automation`` stays the user-facing
    read and still hides deleted rows. None means the row is physically gone.
    """
    reach_params: tuple[str, ...] = ()
    if computer_id is not None:
        reach_params = _last_execution_computer_params(user_id, computer_id)
    async with get_db_connection(conn) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"""
                SELECT {AUTOMATION_COLUMNS}, le.last_execution, {_OWNS_THREAD}
                FROM automations
                {_last_execution_join("automations", computer_id=computer_id)}
                WHERE automation_id = %s AND user_id = %s
            """, (*reach_params, automation_id, user_id))
            result = await cur.fetchone()
            return dict(result) if result else None


async def list_automations(
    user_id: str,
    *,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    computer_id: Optional[str] = None,
    include_deleted: bool = False,
) -> tuple[List[Dict[str, Any]], int]:
    """List automations for a user with optional status filter.

    ``computer_id`` keeps the page to what that computer's token reaches:
    the automations that run in a workspace on it, never one in Home, which
    is out of a computer's reach wherever it sits. ``last_execution`` uses
    that same Home and computer rule on each run's effective workspace.

    Returns:
        Tuple of (list of automation dicts, total count).
    """
    # DALTON-HOOK(company-automations): live rows stay deleted_at IS NULL; the recycle bin flips that predicate only
    deleted_sql = "deleted_at IS NOT NULL" if include_deleted else "deleted_at IS NULL"
    where_parts = ["user_id = %s", deleted_sql]
    params: list = [user_id]

    if status:
        where_parts.append("status = %s")
        params.append(status)
    if computer_id is not None:
        # One with no workspace runs in Home, and the inequality drops it too.
        where_parts.append(
            "automations.workspace_id <> %s::uuid AND EXISTS (SELECT 1 FROM workspaces w"
            " WHERE w.workspace_id = automations.workspace_id"
            " AND w.computer_id = %s::uuid)"
        )
        params += [get_home_workspace_id(user_id), computer_id]

    where_clause = " AND ".join(where_parts)
    # DALTON-HOOK(company-automations): last_execution reach binds before the task WHERE placeholders
    reach_params: tuple[str, ...] = ()
    if computer_id is not None:
        reach_params = _last_execution_computer_params(user_id, computer_id)

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # Get total count
            await cur.execute(
                f"SELECT COUNT(*) as cnt FROM automations WHERE {where_clause}",
                tuple(params),
            )
            total = (await cur.fetchone())["cnt"]

            # Get page. The lateral exposes only ``last_execution``, so the
            # automation columns stay unambiguous without a prefix.
            # DALTON-HOOK(company-automations): deterministic primary-key tiebreaker on pagination
            await cur.execute(f"""
                SELECT {AUTOMATION_COLUMNS}, le.last_execution
                FROM automations
                {_last_execution_join("automations", computer_id=computer_id)}
                WHERE {where_clause}
                ORDER BY created_at DESC, automation_id DESC
                LIMIT %s OFFSET %s
            """, (*reach_params, *params, limit, offset))

            results = await cur.fetchall()
            return [dict(row) for row in results], total


async def list_all_automations(user_id: str, *, conn=None) -> List[Dict[str, Any]]:
    """Every automation of the user, each with its newest run and
    ``owns_thread``: the automations folder, one file per row."""
    async with get_db_connection(conn) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # DALTON-HOOK(company-automations): exclude soft-deleted rows from livefs listing
            await cur.execute(f"""
                SELECT {AUTOMATION_COLUMNS}, le.last_execution, {_OWNS_THREAD}
                FROM automations
                {_last_execution_join("automations")}
                WHERE user_id = %s AND deleted_at IS NULL
                ORDER BY file_name
            """, (user_id,))
            return [dict(row) for row in await cur.fetchall()]


async def get_automation_file(
    user_id: str, file_name: str, *, conn=None, lock: bool = False
) -> Optional[Dict[str, Any]]:
    """The automation filed as ``file_name``, as ``list_all_automations``
    reads each. ``lock`` holds the row for the rest of ``conn``'s
    transaction."""
    async with get_db_connection(conn) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # DALTON-HOOK(company-automations): exclude soft-deleted rows from livefs file read
            await cur.execute(f"""
                SELECT {AUTOMATION_COLUMNS}, le.last_execution, {_OWNS_THREAD}
                FROM automations
                {_last_execution_join("automations")}
                WHERE user_id = %s AND file_name = %s AND deleted_at IS NULL
                {"FOR UPDATE OF automations" if lock else ""}
            """, (user_id, file_name))
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_automation_file_names(user_id: str) -> List[str]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # DALTON-HOOK(company-automations): exclude soft-deleted rows from file name list
            await cur.execute(
                "SELECT file_name FROM automations WHERE user_id = %s AND deleted_at IS NULL ORDER BY file_name",
                (user_id,),
            )
            return [row[0] for row in await cur.fetchall()]


async def rename_automation_file(
    automation_id: str, user_id: str, file_name: str, *, conn
) -> bool:
    """File the automation under another name; nothing else about it
    changes. Raises ``UniqueViolation`` when the user has one there."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE automations SET file_name = %s, updated_at = NOW()
            WHERE automation_id = %s AND user_id = %s
            """,
            (file_name, automation_id, user_id),
        )
        return cur.rowcount > 0


async def get_user_timezone(user_id: str) -> Optional[str]:
    """The zone the user keeps, which a new automation runs on when nothing
    else names one. Only the column: the user row is wide."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT timezone FROM users WHERE user_id = %s", (user_id,))
            row = await cur.fetchone()
            return row[0] if row else None


_FILE_LOCK_KEY_PREFIX = "automations:file:"


async def lock_user_automations(user_id: str, *, conn) -> None:
    """Serialize the writers of the user's automation files for the rest of
    ``conn``'s transaction, even where there is no row to lock: a create, or
    a rename onto a free name.

    The row itself is locked only by a save that changes it
    (``get_automation_file(lock=True)``), which holds off REST edits
    until it commits: a save that changes nothing leaves it alone.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (_FILE_LOCK_KEY_PREFIX + user_id,),
        )


async def update_automation(
    automation_id: str,
    user_id: str,
    *,
    conn=None,
    **kwargs,
) -> Optional[Dict[str, Any]]:
    """Partial update of an automation. Only provided kwargs are applied.

    Fields in ``nullable_fields`` are set even when their value is None
    (i.e. SET column = NULL).  All other fields are skipped when None.
    """
    # DALTON-HOOK(company-automations): extract version check and history recording options
    from datetime import timezone as _dt_tz
    from unittest.mock import NonCallableMock
    from src.dalton.database.company_automations import (
        _to_iso_str,
        lock_and_fetch_user_preferences_for_snapshot,
        prepare_update_history_columns,
    )
    from src.dalton.services.company_automations import (
        UNSET,
        get_current_file_coalesce_state,
        get_current_history_context,
        validate_expected_config_version,
        validate_existing_history_state,
    )

    explicit_expected_cv = kwargs.pop("expected_config_version", UNSET)
    explicit_actor = kwargs.pop("actor_user_id", UNSET)
    explicit_action = kwargs.pop("history_action", None) or kwargs.pop("action", None)
    explicit_record = kwargs.pop("record_history", None)
    explicit_skip_unchanged = kwargs.pop("skip_if_unchanged", None)

    hist_ctx = get_current_history_context()
    nullable_fields = {
        "next_run_at", "last_run_at", "conversation_thread_id", "disable_reason",
        "description", "llm_model", "cron_expression", "updated_by",
    }
    _config_update_fields = {
        "name",
        "description",
        "trigger_type",
        "cron_expression",
        "timezone",
        "instruction",
        "workspace_id",
        "llm_model",
        "thread_strategy",
        "conversation_thread_id",
        "max_failures",
        "trigger_config",
        "additional_context",
        "delivery_config",
        "metadata",
        "scope",
    }
    status_arg = str(kwargs.get("status") or "").strip().lower()
    is_config_update = (
        any(
            k in kwargs and (kwargs[k] is not None or k in nullable_fields)
            for k in _config_update_fields
        )
        or status_arg in ("active", "paused")
        or (
            "next_run_at" in kwargs
            and status_arg not in ("executing", "completed", "disabled")
        )
    )
    should_record = (
        explicit_record
        if explicit_record is not None
        else (
            hist_ctx.record_history
            if hist_ctx is not None
            else is_config_update
        )
    )
    if not isinstance(explicit_expected_cv, type(UNSET)) and explicit_expected_cv is not None:
        expected_cv: Optional[int] = validate_expected_config_version(
            explicit_expected_cv, provided=True
        )
    else:
        expected_cv = hist_ctx.expected_config_version if hist_ctx is not None else None

    if not isinstance(explicit_actor, type(UNSET)):
        eff_actor: Optional[str] = explicit_actor
    elif hist_ctx is not None and hist_ctx.has_explicit_actor:
        eff_actor = hist_ctx.actor_user_id  # type: ignore[assignment]
    elif "updated_by" in kwargs:
        eff_actor = kwargs["updated_by"]
    else:
        eff_actor = user_id

    if explicit_action:
        eff_action = str(explicit_action)
    elif hist_ctx is not None and hist_ctx.has_explicit_action:
        eff_action = str(hist_ctx.action)
    elif kwargs.get("status") == "paused":
        eff_action = "pause"
    elif kwargs.get("status") == "active":
        eff_action = "resume"
    else:
        eff_action = "update"

    eff_skip_unchanged = (
        explicit_skip_unchanged
        if explicit_skip_unchanged is not None
        else (hist_ctx.skip_if_unchanged if hist_ctx is not None else False)
    )

    # DALTON-HOOK(company-automations): allow canonical scope/updated_by persistence and refuse company escalation
    if "scope" in kwargs and str(kwargs["scope"] or "").strip().lower() == "company":
        raise ValueError("Cannot update personal automation scope to company")

    def _build_query_from_kwargs(
        effective_kwargs: Dict[str, Any],
        extra_history_cols: Optional[Dict[str, Any]] = None,
    ) -> UpdateQueryBuilder:
        b = UpdateQueryBuilder()
        for field in [
            "name", "description", "trigger_type", "cron_expression", "timezone",
            "instruction", "workspace_id", "llm_model",
            "thread_strategy", "conversation_thread_id",
            "status", "max_failures", "failure_count", "disable_reason",
            "next_run_at", "last_run_at",
            "scope", "updated_by",
        ]:
            if field not in effective_kwargs:
                continue
            if effective_kwargs[field] is None and field not in nullable_fields:
                continue
            b.add_field(field, effective_kwargs[field], nullable=field in nullable_fields)

        for field in [
            "trigger_config", "additional_context",
            "delivery_config", "metadata",
        ]:
            if field in effective_kwargs and effective_kwargs[field] is not None:
                b.add_field(field, effective_kwargs[field], is_json=True)

        if extra_history_cols:
            if "config_version" in extra_history_cols:
                b.add_field("config_version", extra_history_cols["config_version"])
            if "active_version" in extra_history_cols:
                b.add_field("active_version", extra_history_cols["active_version"])
            if "updated_by" in extra_history_cols:
                b.add_field(
                    "updated_by", extra_history_cols["updated_by"], nullable=True
                )
            if "audit_history" in extra_history_cols:
                b.add_field(
                    "audit_history",
                    extra_history_cols["audit_history"],
                    is_json=True,
                )
        return b

    initial_builder = _build_query_from_kwargs(kwargs)
    if not initial_builder.has_updates() and expected_cv is None:
        return await get_automation(automation_id, user_id, conn=conn)

    returning = AUTOMATION_COLUMNS.strip().split(",")
    returning = [c.strip() for c in returning]

    async with get_db_connection(conn) as conn:
        is_mock_conn = isinstance(conn, NonCallableMock)
        if not is_mock_conn and (should_record or expected_cv is not None):
            now_iso = datetime.now(_dt_tz.utc).isoformat()
            async with conn.transaction():
                # DALTON-HOOK(company-automations): lock user_preferences inside transaction before locking automations row
                coalesce = get_current_file_coalesce_state()
                if should_record and not (
                    coalesce is not None and coalesce.mode == "create_pause"
                ):
                    if (
                        coalesce is not None
                        and coalesce.prelocked_pref_row is not None
                    ):
                        pref_row = coalesce.prelocked_pref_row
                    elif (
                        hist_ctx is not None
                        and hist_ctx.prelocked_pref_row is not None
                    ):
                        pref_row = hist_ctx.prelocked_pref_row
                    else:
                        pref_row = await lock_and_fetch_user_preferences_for_snapshot(
                            user_id,
                            conn=conn,
                            notify_storage_mode="legacy",
                        )
                else:
                    pref_row = {}
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"""
                        SELECT {AUTOMATION_COLUMNS}, audit_history
                        FROM automations
                        WHERE automation_id = %s AND user_id = %s AND deleted_at IS NULL
                        FOR UPDATE
                        """,
                        (automation_id, user_id),
                    )
                    locked_row = await cur.fetchone()
                    if not locked_row:
                        return None

                    if not (coalesce is not None and coalesce.mode == "create_pause"):
                        validate_existing_history_state(locked_row)
                        if (
                            expected_cv is not None
                            and (
                                coalesce is None
                                or coalesce.mode != "update_action"
                                or coalesce.step1_pre_row is None
                            )
                            and int(locked_row.get("config_version") or 1) != expected_cv
                        ):
                            from src.dalton.services.company_automations import (
                                AutomationVersionConflictError,
                            )

                            raise AutomationVersionConflictError(
                                f"Configuration version conflict: expected {expected_cv}, current is {locked_row.get('config_version')}"
                            )

                    effective_kwargs = dict(kwargs)
                    if (
                        hist_ctx is not None
                        and hist_ctx.locked_row_reconciler is not None
                    ):
                        await hist_ctx.locked_row_reconciler(
                            locked_row, effective_kwargs, conn
                        )

                    target_trigger = str(
                        effective_kwargs.get("trigger_type")
                        or locked_row.get("trigger_type")
                        or ""
                    ).strip()
                    if (
                        target_trigger == "once"
                        and "next_run_at" in effective_kwargs
                        and effective_kwargs["next_run_at"] is not None
                    ):
                        once_iso = _to_iso_str(effective_kwargs["next_run_at"])
                        if once_iso is not None:
                            merged_meta = dict(locked_row.get("metadata") or {})
                            if isinstance(effective_kwargs.get("metadata"), dict):
                                merged_meta.update(effective_kwargs["metadata"])
                            merged_meta["once_schedule_at"] = once_iso
                            effective_kwargs["metadata"] = merged_meta

                    extra_hist_cols: Dict[str, Any] = {}

                    if not should_record:
                        pass
                    elif coalesce is not None and coalesce.mode == "create_pause":
                        # Create + pause inside AutomationFile already recorded v1 with desired_status='paused'
                        pass
                    elif (
                        coalesce is not None
                        and coalesce.mode == "update_action"
                        and coalesce.step1_pre_row is None
                    ):
                        import copy

                        coalesce.step1_pre_row = copy.deepcopy(dict(locked_row))
                        coalesce.step1_pref_row = pref_row
                    else:
                        base_pre_row = (
                            coalesce.step1_pre_row
                            if (
                                coalesce is not None
                                and coalesce.mode == "update_action"
                                and coalesce.step1_pre_row is not None
                            )
                            else None
                        )
                        combined_action = (
                            "update" if base_pre_row is not None else eff_action
                        )
                        eff_pref = (
                            coalesce.step1_pref_row
                            if (
                                coalesce is not None
                                and coalesce.step1_pref_row is not None
                            )
                            else pref_row
                        )
                        extra_hist_cols, is_noop = await prepare_update_history_columns(
                            locked_row,
                            effective_kwargs,
                            actor_user_id=eff_actor,
                            action=combined_action,
                            expected_config_version=expected_cv,
                            skip_if_unchanged=eff_skip_unchanged,
                            pref_row=eff_pref,
                            base_pre_row=base_pre_row,
                            changed_at_iso=now_iso,
                            conn=conn,
                        )
                        if is_noop:
                            extra_hist_cols = {}
                            effective_kwargs.pop("updated_by", None)

                    builder = _build_query_from_kwargs(
                        effective_kwargs, extra_hist_cols
                    )
                    if not builder.has_updates():
                        return await get_automation(
                            automation_id, user_id, conn=conn
                        )

                    # DALTON-HOOK(company-automations): only update active (non-soft-deleted) rows
                    query, params = builder.build(
                        table="automations",
                        where_clause="automation_id = %s AND user_id = %s AND deleted_at IS NULL",
                        where_params=[automation_id, user_id],
                        returning_columns=returning,
                    )
                    query = f"""
                        WITH updated AS ({query})
                        SELECT updated.*, le.last_execution
                        FROM updated
                        {_last_execution_join("updated")}
                    """
                    await cur.execute(query, params)
                    result = await cur.fetchone()
                    if result:
                        logger.info(
                            f"[automation_db] update_automation automation_id={automation_id}"
                        )
                    return dict(result) if result else None

        # Fallback for mock connections or non-history runtime updates
        builder = _build_query_from_kwargs(kwargs)
        if not builder.has_updates():
            return await get_automation(automation_id, user_id, conn=conn)

        query, params = builder.build(
            table="automations",
            where_clause="automation_id = %s AND user_id = %s AND deleted_at IS NULL",
            where_params=[automation_id, user_id],
            returning_columns=returning,
        )
        query = f"""
            WITH updated AS ({query})
            SELECT updated.*, le.last_execution
            FROM updated
            {_last_execution_join("updated")}
        """
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            result = await cur.fetchone()
            if result:
                logger.info(f"[automation_db] update_automation automation_id={automation_id}")
            return dict(result) if result else None


async def delete_automation(
    automation_id: str,
    user_id: str,
    *,
    conn=None,
    actor_user_id: str | None = None,
    expected_config_version: int | None = None,
    prelocked_pref_row=None,
) -> bool:
    """Soft-delete an automation. Physical removal waits for the 30-day purge.

    DALTON-HOOK(company-automations): recycle-bin soft delete. The row stays,
    pending and waiting runs are skipped, and executions are not cascaded.
    ``expected_config_version is None`` uses the locked row's own version.
    """
    from src.dalton.services.automation_recycle import soft_delete_owned_automation

    deleted = await soft_delete_owned_automation(
        automation_id,
        user_id,
        actor_user_id=actor_user_id,
        expected_config_version=expected_config_version,
        prelocked_pref_row=prelocked_pref_row,
        conn=conn,
    )
    if deleted:
        logger.info(f"[automation_db] delete_automation automation_id={automation_id}")
    return deleted


# =============================================================================
# Scheduler queries (used by AutomationScheduler)
# =============================================================================


async def claim_due_automations(
    now: datetime,
    server_id: str,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Atomically claim automations whose next_run_at <= now.

    Uses FOR UPDATE SKIP LOCKED so multiple server instances
    won't double-claim the same automation.

    For each claimed row:
    - Sets next_run_at to NULL (will be recalculated externally)
    - Sets last_run_at to the claim's transaction time
    - Inserts a pending execution record

    ``last_run_at`` and the execution's ``created_at`` are the same NOW(),
    which is how settling a one-time firing tells the claimed firing from a
    manual run of the same automation.

    Returns the claimed automation rows together with the new execution_id.
    """
    async with get_db_connection() as conn:
        # Need an explicit transaction (autocommit is ON by default)
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                # Lock and fetch due automations
                await cur.execute(f"""
                    SELECT {AUTOMATION_COLUMNS}
                    FROM automations
                    WHERE status = 'active'
                      AND deleted_at IS NULL
                      AND next_run_at IS NOT NULL
                      AND next_run_at <= %s
                    ORDER BY next_run_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                """, (now, limit))

                rows = await cur.fetchall()
                if not rows:
                    return []

                from src.dalton.database.company_automations import _to_iso_str  # DALTON-HOOK(company-automations)

                claimed = []
                for row in rows:
                    automation_id = str(row["automation_id"])
                    execution_id = str(uuid4())
                    once_iso = _to_iso_str(row.get("next_run_at"))

                    # DALTON-HOOK(company-automations): preserve once_schedule_at in metadata before clearing next_run_at
                    # Advance next_run_at to NULL (scheduler will recalculate)
                    if str(row.get("trigger_type") or "") == "once" and once_iso:
                        await cur.execute("""
                            UPDATE automations
                            SET metadata = COALESCE(metadata, '{}'::jsonb)
                                    || jsonb_build_object('once_schedule_at', %s::text),
                                next_run_at = NULL,
                                last_run_at = NOW()
                            WHERE automation_id = %s
                              AND deleted_at IS NULL
                        """, (once_iso, automation_id))
                    else:
                        await cur.execute("""
                            UPDATE automations
                            SET next_run_at = NULL, last_run_at = NOW()
                            WHERE automation_id = %s
                              AND deleted_at IS NULL
                        """, (automation_id,))

                    # Insert pending execution
                    await cur.execute("""
                        INSERT INTO automation_executions (
                            automation_execution_id, automation_id,
                            status, scheduled_at, server_id, created_at,
                            heartbeat_at
                        )
                        VALUES (%s, %s, 'pending', %s, %s, NOW(), NOW())
                    """, (execution_id, automation_id, row["next_run_at"], server_id))

                    entry = dict(row)
                    # DALTON-HOOK(company-automations): reflect once_schedule_at on returned claimed entry
                    if str(row.get("trigger_type") or "") == "once" and once_iso:
                        merged_meta = dict(entry.get("metadata") or {})
                        merged_meta["once_schedule_at"] = once_iso
                        entry["metadata"] = merged_meta
                    entry["_execution_id"] = execution_id
                    claimed.append(entry)

                logger.info(
                    f"[automation_db] claimed {len(claimed)} due automations "
                    f"(server_id={server_id})"
                )
                return claimed


async def claim_price_firing(automation_id: str, server_id: str) -> Optional[str]:
    """Claim a price alert whose condition just hit; the new execution_id, or
    None when the alert is no longer active.

    The monitor decides from a list it loaded earlier, so the claim itself
    checks the status: a pause made since then wins instead of being flipped
    to 'executing' and re-armed to 'active' after the run. ``last_run_at`` and
    the execution's ``created_at`` share one NOW(), as in
    ``claim_due_automations``, so settling can tell this firing from a manual
    run of the same alert.
    """
    execution_id = str(uuid4())
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE automations
                    SET status = 'executing', next_run_at = NULL, last_run_at = NOW()
                    WHERE automation_id = %s AND status = 'active'
                      AND deleted_at IS NULL
                """, (automation_id,))
                if cur.rowcount == 0:
                    return None
                await cur.execute("""
                    INSERT INTO automation_executions (
                        automation_execution_id, automation_id,
                        status, scheduled_at, server_id, created_at,
                        heartbeat_at
                    )
                    VALUES (%s, %s, 'pending', NOW(), %s, NOW(), NOW())
                """, (execution_id, automation_id, server_id))
    return execution_id


async def update_automation_next_run(
    automation_id: str,
    next_run_at: Optional[datetime],
    *,
    config_version: int | None = None,
) -> bool:
    """Schedule a claimed cron's next run, unless the row moved on meanwhile.

    The claim left ``next_run_at`` NULL. An edit to the schedule or zone made
    since has already set it from the new expression, and a pause cleared
    the status; either way the time computed from the claimed row is stale,
    so it is written only over a NULL on a row still active. A soft-deleted
    row is never rescheduled. ``config_version`` keeps a stale claim from
    writing over a newer edit.

    DALTON-HOOK(company-automations): recycle bin refuses a deleted row here.
    """
    version_sql = ""
    params: list = [next_run_at, automation_id]
    if config_version is not None:
        version_sql = " AND config_version = %s"
        params.append(config_version)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"""
                UPDATE automations
                SET next_run_at = %s
                WHERE automation_id = %s
                  AND next_run_at IS NULL
                  AND status = 'active'
                  AND deleted_at IS NULL
                  {version_sql}
            """, tuple(params))
            return cur.rowcount > 0


async def get_active_price_automations() -> List[Dict[str, Any]]:
    """Get all active price-triggered automations (for PriceMonitorService)."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"""
                SELECT {AUTOMATION_COLUMNS}
                FROM automations
                WHERE trigger_type = 'price'
                  AND status = 'active'
                  AND deleted_at IS NULL
            """)
            results = await cur.fetchall()
            return [dict(row) for row in results]
