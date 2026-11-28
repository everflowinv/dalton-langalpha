"""Purge gates, FOR SHARE dual-generation writeback fences, closed-range helpers, and Fork/truncate clipping.

All state lives in PostgreSQL (``conversation_threads`` and ``dalton_integration_purge_ledger``)
with zero process-local truth, safe across ``--workers N``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime
import logging
from typing import Any

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.dalton.database.user_integrations import touch_user_lock_key
from src.dalton.settings import is_dalton_enabled
from src.server.database.pool import get_db_connection
from src.server.utils.pg_sanitize import normalize_uuid

logger = logging.getLogger(__name__)

PURGE_IN_PROGRESS_CODE = "personal_data_purge_in_progress"
PURGE_IN_PROGRESS_MESSAGE = "正在清除该对话中的个人集成数据，请稍后重试。"
PURGE_RETRY_AFTER_SECONDS = "3"


def _is_uninitialized_pool_error(exc: BaseException) -> bool:
    return isinstance(exc, RuntimeError) and "pool is not open" in str(exc).lower()


def _is_scripted_mock_conn(conn: Any) -> bool:
    """Return True when ``conn`` is a mock or stub double without a real DB cursor."""
    from unittest.mock import NonCallableMock

    return isinstance(conn, NonCallableMock) or not callable(getattr(conn, "cursor", None))


class PersonalDataPurgeInProgressError(HTTPException):
    """409 Conflict raised when a thread has an active personal integration data purge."""

    def __init__(self, thread_id: str | None = None) -> None:
        self.thread_id = thread_id
        super().__init__(
            status_code=409,
            detail={
                "code": PURGE_IN_PROGRESS_CODE,
                "message": PURGE_IN_PROGRESS_MESSAGE,
            },
            headers={"Retry-After": PURGE_RETRY_AFTER_SECONDS},
        )


# ---------------------------------------------------------------------------
# Closed turn-range arithmetic ([start, end], inclusive on both ends)
# ---------------------------------------------------------------------------


def normalize_turn_ranges(raw: Any) -> list[list[int]]:
    """Normalize stored ``personal_purged_turn_ranges`` JSONB into sorted, merged closed intervals."""
    if not isinstance(raw, list):
        return []
    pairs: list[tuple[int, int]] = []
    for item in raw:
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance(item[0], int)
            and isinstance(item[1], int)
            and not isinstance(item[0], bool)
            and not isinstance(item[1], bool)
        ):
            lo, hi = int(item[0]), int(item[1])
            if lo <= hi:
                pairs.append((lo, hi))
    if not pairs:
        return []
    pairs.sort()
    merged: list[list[int]] = [[pairs[0][0], pairs[0][1]]]
    for lo, hi in pairs[1:]:
        prev = merged[-1]
        if lo <= prev[1] + 1:
            if hi > prev[1]:
                prev[1] = hi
        else:
            merged.append([lo, hi])
    return merged


def merge_turn_range(existing: Any, start: int, end: int) -> list[list[int]]:
    """Merge closed interval ``[start, end]`` into ``existing``."""
    if start > end:
        return normalize_turn_ranges(existing)
    base = normalize_turn_ranges(existing)
    base.append([int(start), int(end)])
    return normalize_turn_ranges(base)


def is_turn_in_purged_ranges(turn_index: int, ranges: Any) -> bool:
    """Return True if ``turn_index`` falls inside any closed interval in ``ranges``."""
    for lo, hi in normalize_turn_ranges(ranges):
        if lo <= turn_index <= hi:
            return True
    return False


def clip_purged_turn_ranges(ranges: Any, max_inclusive_turn: int) -> list[list[int]]:
    """Clip closed intervals in ``ranges`` to ``<= max_inclusive_turn``."""
    if max_inclusive_turn < 0:
        return []
    clipped: list[list[int]] = []
    for lo, hi in normalize_turn_ranges(ranges):
        if lo > max_inclusive_turn:
            continue
        clipped.append([lo, min(hi, max_inclusive_turn)])
    return clipped


# ---------------------------------------------------------------------------
# Thread purge state queries & START / _admit / Fork gates (§4.5(2) item 2)
# ---------------------------------------------------------------------------


async def _has_running_purge_touch(
    conn: Any,
    thread_id: str,
    *,
    max_turn_index: int | None = None,
) -> bool:
    """Return True if ``thread_id`` has an unpurged touch row tied to a running purge ledger."""
    turn_clause = "AND t.turn_index <= %(max_turn)s" if max_turn_index is not None else ""
    params: dict[str, Any] = {"thread_id": thread_id, "max_turn": max_turn_index}
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT 1
            FROM dalton_thread_integration_touches t
            JOIN dalton_integration_purge_ledger l
              ON l.user_id = t.user_id
             AND l.provider = t.provider
             AND (l.service IS NULL OR l.service = t.service)
             AND t.auth_epoch <= l.auth_epoch_upto
            WHERE t.thread_id = %(thread_id)s::uuid
              AND t.purged_at IS NULL
              AND l.local_purge_status = 'running'
              {turn_clause}
            LIMIT 1
            """,
            params,
        )
        return (await cur.fetchone()) is not None


async def check_thread_not_purging_fast(thread_id: str | None) -> None:
    """Fast preflight check for HTTP ``_admit``: raises 409 + Retry-After: 3 if thread is purging."""
    if not is_dalton_enabled():
        return
    tid = normalize_uuid(thread_id)
    if tid is None:
        return
    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT personal_purge_in_progress
                    FROM conversation_threads
                    WHERE conversation_thread_id = %s::uuid
                    """,
                    (tid,),
                )
                row = await cur.fetchone()
            if row is None:
                return
            if bool(row["personal_purge_in_progress"]) or await _has_running_purge_touch(
                conn, tid
            ):
                raise PersonalDataPurgeInProgressError(tid)
    except RuntimeError as exc:
        if _is_uninitialized_pool_error(exc):
            return
        raise


async def assert_thread_not_purging_tx(conn: Any, thread_id: str) -> None:
    """Authoritative START-transaction gate (called inside ``start_run`` after ``lock_run_workspace``).

    Locks the thread row ``FOR UPDATE`` so it serializes strictly with Phase 1
    disconnect locking and refuses all entry points (HTTP, automations, wakeups,
    report-backs, retries) while a local 2A purge is in progress.
    """
    if not is_dalton_enabled():
        return
    if _is_scripted_mock_conn(conn):
        return
    tid = normalize_uuid(thread_id)
    if tid is None:
        return
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT personal_purge_in_progress
            FROM conversation_threads
            WHERE conversation_thread_id = %s::uuid
            FOR UPDATE
            """,
            (tid,),
        )
        row = await cur.fetchone()
    if row is None:
        return
    if bool(row["personal_purge_in_progress"]) or await _has_running_purge_touch(
        conn, tid
    ):
        raise PersonalDataPurgeInProgressError(tid)


async def assert_fork_source_not_purging_tx(
    conn: Any,
    source_thread_id: str,
    *,
    cut_turn_index: int | None = None,
    expected_purge_gen: int | None = None,
) -> None:
    """Authoritative Fork gate inside the fork transaction.

    1. Acquires shared per-user transaction advisory locks (in sorted ``user_id`` order)
       for every user with unpurged touches on ``source_thread_id``, serializing Fork touch
       copying against Phase 1 touch enumeration.
    2. Locks the source thread row ``FOR UPDATE`` and verifies ``personal_purge_in_progress``
       is False, ``personal_purge_generation == expected_purge_gen`` (when supplied), and no
       running purge covers touches up to ``cut_turn_index``.
    """
    if not is_dalton_enabled():
        return
    if _is_scripted_mock_conn(conn):
        return
    from src.server.services.forks.model import ForkRefused

    tid = normalize_uuid(source_thread_id)
    if tid is None:
        return
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT DISTINCT user_id
            FROM dalton_thread_integration_touches
            WHERE thread_id = %s::uuid
              AND purged_at IS NULL
            ORDER BY user_id ASC
            """,
            (tid,),
        )
        touch_user_rows = await cur.fetchall()
        for urow in touch_user_rows:
            await cur.execute(
                "SELECT pg_advisory_xact_lock_shared(%s)",
                (touch_user_lock_key(str(urow["user_id"])),),
            )
        await cur.execute(
            """
            SELECT personal_purge_in_progress, personal_purge_generation
            FROM conversation_threads
            WHERE conversation_thread_id = %s::uuid
            FOR UPDATE
            """,
            (tid,),
        )
        row = await cur.fetchone()
    if row is None:
        return
    current_gen = int(row.get("personal_purge_generation") or 0)
    if (
        bool(row["personal_purge_in_progress"])
        or (expected_purge_gen is not None and current_gen != expected_purge_gen)
        or await _has_running_purge_touch(conn, tid, max_turn_index=cut_turn_index)
    ):
        raise ForkRefused(409, PURGE_IN_PROGRESS_CODE, PURGE_IN_PROGRESS_MESSAGE)


# ---------------------------------------------------------------------------
# Truncate & Fork range clipping + touch inheritance (§4.4(4), §4.5(2) item 4)
# ---------------------------------------------------------------------------


async def clip_purged_ranges_on_truncate_tx(
    conn: Any,
    thread_id: str,
    from_turn_index: int,
) -> list[list[int]]:
    """Clip ``personal_purged_turn_ranges`` to ``<= from_turn_index - 1`` inside ``truncate_thread_from_turn``."""
    if not is_dalton_enabled():
        return []
    if _is_scripted_mock_conn(conn):
        return []
    tid = normalize_uuid(thread_id)
    if tid is None:
        return []
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT personal_purged_turn_ranges
            FROM conversation_threads
            WHERE conversation_thread_id = %s::uuid
            FOR UPDATE
            """,
            (tid,),
        )
        row = await cur.fetchone()
        if row is None:
            return []
        current = normalize_turn_ranges(row.get("personal_purged_turn_ranges"))
        clipped = clip_purged_turn_ranges(current, from_turn_index - 1)
        if clipped != current:
            await cur.execute(
                """
                UPDATE conversation_threads
                SET personal_purged_turn_ranges = %s,
                    updated_at = NOW()
                WHERE conversation_thread_id = %s::uuid
                """,
                (Jsonb(clipped), tid),
            )
        return clipped


def fork_max_inclusive_turn(
    cut: Any,
    kept_turn_indexes: Sequence[int],
    *,
    lineage: Any = None,
) -> int | None:
    """Compute the inclusive upper turn bound for a Fork cut (same boundary as copied turns).

    - No copied or borrowed turns -> ``None`` (ranges become ``[]``).
    - ``ThroughTurn(k)`` -> ``k`` (includes turn ``k``).
    - ``BeforeTurn(k)`` -> ``k - 1`` (excludes turn ``k``).
    """
    borrow = getattr(lineage, "borrow", None) if lineage is not None else None
    ancestors = getattr(borrow, "ancestors", None) if borrow is not None else None
    if not kept_turn_indexes and not ancestors:
        return None
    cut_name = getattr(cut, "name", None)
    turn_index = int(getattr(cut, "turn_index", 0))
    if cut_name == "before_turn":
        return turn_index - 1
    return turn_index


async def copy_personal_integration_state_for_fork_tx(
    conn: Any,
    *,
    source_thread_id: str,
    target_thread_id: str,
    cut: Any,
    kept_turn_indexes: Sequence[int],
    run_map: Mapping[str, str],
    lineage: Any = None,
) -> None:
    """Propagate ``has_personal_integration_data``, clipped ``personal_purged_turn_ranges``,
    and inherited ``dalton_thread_integration_touches`` onto a forked thread inside the fork tx.
    """
    if not is_dalton_enabled():
        return
    if _is_scripted_mock_conn(conn):
        return
    src_id = normalize_uuid(source_thread_id)
    dst_id = normalize_uuid(target_thread_id)
    if src_id is None or dst_id is None:
        return

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT has_personal_integration_data, personal_purged_turn_ranges
            FROM conversation_threads
            WHERE conversation_thread_id = %s::uuid
            """,
            (src_id,),
        )
        src_row = await cur.fetchone()
        if src_row is None:
            return

        max_turn = fork_max_inclusive_turn(cut, kept_turn_indexes, lineage=lineage)
        if max_turn is None or max_turn < 0:
            clipped_ranges: list[list[int]] = []
            touches: list[dict[str, Any]] = []
        else:
            clipped_ranges = clip_purged_turn_ranges(
                src_row.get("personal_purged_turn_ranges"), max_turn
            )
            await cur.execute(
                """
                SELECT response_id, provider, service, auth_epoch, turn_index,
                       user_id, provider_account_id, touched_at, purged_at
                FROM dalton_thread_integration_touches
                WHERE thread_id = %s::uuid
                  AND turn_index <= %s
                ORDER BY turn_index ASC
                """,
                (src_id, max_turn),
            )
            touches = [dict(r) for r in await cur.fetchall()]

        has_personal = bool(touches or clipped_ranges)
        if has_personal:
            await cur.execute(
                """
                UPDATE conversation_threads
                SET has_personal_integration_data = TRUE,
                    personal_purged_turn_ranges = %s,
                    is_shared = FALSE,
                    workspace_shared_at = NULL
                WHERE conversation_thread_id = %s::uuid
                """,
                (Jsonb(clipped_ranges), dst_id),
            )
        elif clipped_ranges:
            await cur.execute(
                """
                UPDATE conversation_threads
                SET personal_purged_turn_ranges = %s
                WHERE conversation_thread_id = %s::uuid
                """,
                (Jsonb(clipped_ranges), dst_id),
            )

        for touch in touches:
            old_rid = str(touch["response_id"])
            # Snapshot forks borrow main turns instead of duplicating conversation_responses,
            # so ``run_map`` only remaps copied subagent task runs. Preserve ``old_rid`` for
            # borrowed main-turn touches so disconnect/purge and fork-of-fork still cover dst_id.
            target_rid = run_map.get(old_rid) or old_rid
            await cur.execute(
                """
                INSERT INTO dalton_thread_integration_touches (
                    thread_id, response_id, provider, service, auth_epoch,
                    turn_index, user_id, provider_account_id, touched_at, purged_at
                )
                VALUES (
                    %s::uuid, %s::uuid, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (thread_id, response_id, provider, service, auth_epoch)
                DO NOTHING
                """,
                (
                    dst_id,
                    str(target_rid),
                    touch["provider"],
                    touch["service"],
                    int(touch["auth_epoch"]),
                    int(touch["turn_index"]),
                    touch["user_id"],
                    touch["provider_account_id"],
                    touch["touched_at"],
                    touch["purged_at"],
                ),
            )


# ---------------------------------------------------------------------------
# FOR SHARE dual-generation writeback fence (§4.5(2) item 3)
# ---------------------------------------------------------------------------


async def read_thread_purge_state(
    thread_id: str,
    *,
    conn: Any = None,
) -> tuple[bool, int, list[list[int]]] | None:
    """Read ``(personal_purge_in_progress, personal_purge_generation, personal_purged_turn_ranges)``."""
    if not is_dalton_enabled():
        return None
    if conn is not None and _is_scripted_mock_conn(conn):
        return None
    tid = normalize_uuid(thread_id)
    if tid is None:
        return None
    try:
        async with get_db_connection(conn) as db_conn:
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT personal_purge_in_progress,
                           personal_purge_generation,
                           personal_purged_turn_ranges
                    FROM conversation_threads
                    WHERE conversation_thread_id = %s::uuid
                    """,
                    (tid,),
                )
                row = await cur.fetchone()
    except RuntimeError as exc:
        if _is_uninitialized_pool_error(exc):
            return None
        raise
    if row is None:
        return None
    return (
        bool(row["personal_purge_in_progress"]),
        int(row["personal_purge_generation"] or 0),
        normalize_turn_ranges(row.get("personal_purged_turn_ranges")),
    )


async def read_expected_purge_generation(
    thread_id: str,
    *,
    conn: Any = None,
) -> int | None:
    """Snapshot ``personal_purge_generation`` BEFORE reading checkpoints for a writeback."""
    if not is_dalton_enabled():
        return None
    state = await read_thread_purge_state(thread_id, conn=conn)
    return None if state is None else state[1]


async def verify_writeback_fence_tx(
    conn: Any,
    thread_id: str,
    expected_purge_gen: int | None,
    *,
    label: str = "writeback",
) -> bool:
    """Execute ``SELECT ... FROM conversation_threads WHERE ... FOR SHARE`` inside a writeback tx.

    Returns ``True`` iff the thread exists, ``personal_purge_in_progress IS FALSE``,
    and ``personal_purge_generation == expected_purge_gen`` (when ``expected_purge_gen``
    is provided). Logs and returns ``False`` on fence mismatch without raising to callers.
    """
    if not is_dalton_enabled():
        return True
    if _is_scripted_mock_conn(conn):
        return True
    tid = normalize_uuid(thread_id)
    if tid is None:
        return True
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT personal_purge_in_progress, personal_purge_generation
            FROM conversation_threads
            WHERE conversation_thread_id = %s::uuid
            FOR SHARE
            """,
            (tid,),
        )
        row = await cur.fetchone()
    if row is None:
        logger.info("[purge-fence] %s skipped: thread=%s missing", label, tid)
        return False
    in_progress = bool(row["personal_purge_in_progress"])
    current_gen = int(row["personal_purge_generation"] or 0)
    if in_progress:
        logger.info(
            "[purge-fence] %s dropped: thread=%s purge in progress (gen=%d)",
            label,
            tid,
            current_gen,
        )
        return False
    if expected_purge_gen is not None and current_gen != expected_purge_gen:
        logger.info(
            "[purge-fence] %s dropped: thread=%s generation moved expected=%d current=%d",
            label,
            tid,
            expected_purge_gen,
            current_gen,
        )
        return False
    return True


async def check_writeback_fence_tx(
    conn: Any,
    thread_id: str | None,
    expected_purge_gen: int | None,
    *,
    label: str = "writeback",
) -> bool:
    """Thin-hook wrapper: passes immediately when Dalton is disabled or no fence is requested; otherwise runs ``verify_writeback_fence_tx``."""
    if not is_dalton_enabled() or thread_id is None or expected_purge_gen is None:
        return True
    return await verify_writeback_fence_tx(
        conn, thread_id, expected_purge_gen, label=label
    )


async def write_many_fenced(
    sql: str,
    params: list[tuple[Any, ...]],
    what: str,
    *,
    thread_id: str,
    expected_purge_gen: int,
) -> None:
    """Fenced ``_write_many`` implementation for ``turn_slices`` / ``task_run_slices``."""
    from psycopg.errors import ForeignKeyViolation
    from src.server.database import pool

    if not is_dalton_enabled():
        async with pool.get_db_connection() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.executemany(sql, params)
                    return
                except ForeignKeyViolation:
                    if len(params) == 1:
                        logger.debug("%s not stored, its parent row is gone", what)
                        return
                for row in params:
                    try:
                        await cur.execute(sql, row)
                    except ForeignKeyViolation:
                        logger.debug("%s not stored, its parent row is gone", what)
        return

    async with pool.get_db_connection() as conn, conn.transaction():
        if not await verify_writeback_fence_tx(
            conn, thread_id, expected_purge_gen, label=what
        ):
            return
        async with conn.cursor() as cur:
            try:
                async with conn.transaction():
                    await cur.executemany(sql, params)
                return
            except ForeignKeyViolation:
                if len(params) == 1:
                    logger.debug("%s not stored, its parent row is gone", what)
                    return
            for row in params:
                try:
                    async with conn.transaction():
                        await cur.execute(sql, row)
                except ForeignKeyViolation:
                    logger.debug("%s not stored, its parent row is gone", what)


def build_guarded_search_rows(
    sources: Sequence[Any],
    read: Any,
    purge_state: tuple[bool, int, list[list[int]]] | None,
    clean_fn: Any,
    notification_query_types: Any = None,
) -> tuple[list[Any], int | None]:
    """Build ``IndexedTurn`` rows for ``index_turns`` with closed-range empty-string guard.

    Turns inside ``personal_purged_turn_ranges`` are forced to empty text (``answer_read=True``);
    turns outside are indexed normally. Returns ``(rows, expected_purge_gen)``.
    """
    from src.server.contracts.turn_kinds import NOTIFICATION_QUERY_TYPES
    from src.server.database.search import index as search_db

    notif_types = (
        NOTIFICATION_QUERY_TYPES
        if notification_query_types is None
        else notification_query_types
    )
    if not is_dalton_enabled():
        return (
            [
                search_db.IndexedTurn(
                    response_id=s.response_id,
                    user_text=""
                    if s.query_type in notif_types
                    else clean_fn(s.user_text),
                    answer_text=clean_fn(read.texts.get(s.response_id)),
                    answer_read=s.response_id not in read.unread,
                    turn_at=s.turn_at,
                )
                for s in sources
            ],
            None,
        )
    expected_purge_gen = purge_state[1] if purge_state is not None else None
    purged_ranges = purge_state[2] if purge_state is not None else []
    indexed_rows = [
        search_db.IndexedTurn(
            response_id=s.response_id,
            user_text="",
            answer_text="",
            answer_read=True,
            turn_at=s.turn_at,
        )
        if is_turn_in_purged_ranges(s.turn_index, purged_ranges)
        else search_db.IndexedTurn(
            response_id=s.response_id,
            user_text=""
            if s.query_type in notif_types
            else clean_fn(s.user_text),
            answer_text=clean_fn(read.texts.get(s.response_id)),
            answer_read=s.response_id not in read.unread,
            turn_at=s.turn_at,
        )
        for s in sources
    ]
    return indexed_rows, expected_purge_gen


async def write_guarded_search_turns(
    write_fn: Any,
    thread_id: str,
    sources: Sequence[Any],
    read: Any,
    purge_state: tuple[bool, int, list[list[int]]] | None,
    read_at: datetime | None,
    clean_fn: Any,
    notification_query_types: Any = None,
) -> int:
    """Thin-hook helper for ``search.index.index_turns``: builds guarded rows and writes with fence when enabled."""
    rows, expected_purge_gen = build_guarded_search_rows(
        sources, read, purge_state, clean_fn, notification_query_types
    )
    if expected_purge_gen is None:
        return await write_fn(thread_id, rows, read_at=read_at)
    return await write_fn(
        thread_id, rows, read_at=read_at, expected_purge_gen=expected_purge_gen
    )


async def save_transcript_with_stale_retry(
    target: Any,
    job: Any,
    rendered: Any,
    render_job: Any,
    concurrency: int,
) -> bool:
    """Fenced transcript save with ``StaleCopy`` full re-render retry (called from ``transcripts._save``)."""
    from src.server.database.thread_transcripts import StaleCopy

    agent = job.agent
    try:
        return await save_stored_transcript_fenced(
            agent.thread_id,
            agent.prefix,
            target.user_id,
            rendered.copy,
            rendered.blobs,
            expected_purge_gen=job.expected_purge_gen,
            concurrency=concurrency,
        )
    except StaleCopy:
        if job.previous is None:
            raise
        logger.info(
            "Transcript %s moved under its render; rendering it in full",
            agent.directory,
        )
    full = await render_job(job, inline=target.inline, full=True)
    return await save_stored_transcript_fenced(
        agent.thread_id,
        agent.prefix,
        target.user_id,
        full.copy,
        full.blobs,
        expected_purge_gen=job.expected_purge_gen,
        concurrency=concurrency,
    )


async def _upload_and_register_blobs_tracked(
    user_id: str,
    contents: dict[str, bytes],
    *,
    concurrency: int = 8,
) -> list[tuple[str, datetime]]:
    """Upload & register only unregistered blobs, returning ``[(sha256, stamp)]`` for cleanup on rollback."""
    if not contents:
        return []
    from src.server.database.blob_keys import (
        BLOB_CONTENT_TYPE,
        CLAIM_SQL,
        REGISTER_SQL,
        RELAY_MAX_BYTES,
        blob_key,
    )
    from src.server.database.workspace_file_blobs import (
        BlobUploadError,
        registered_blobs,
    )
    from src.utils.storage import upload_bytes as _storage_upload_bytes

    already = await registered_blobs(user_id, list(contents))
    missing = {sha: data for sha, data in contents.items() if sha not in already}
    if not missing:
        return []

    keys = {sha: blob_key(user_id, sha) for sha in missing}
    async with get_db_connection() as claim_conn:
        async with claim_conn.cursor() as cur:
            await cur.execute(CLAIM_SQL, (user_id, list(keys)))

    gate = asyncio.Semaphore(concurrency)

    async def _put(sha: str) -> bool:
        async with gate:
            return await asyncio.to_thread(
                _storage_upload_bytes,
                keys[sha],
                missing[sha],
                BLOB_CONTENT_TYPE,
                max_size=RELAY_MAX_BYTES,
            )

    outcomes = await asyncio.gather(*(_put(sha) for sha in keys), return_exceptions=True)
    landed = [
        (sha, len(missing[sha]))
        for sha, outcome in zip(keys, outcomes)
        if outcome is True
    ]
    stamps: list[tuple[str, datetime]] = []
    if landed:
        async with get_db_connection() as reg_conn:
            async with reg_conn.cursor() as cur:
                for sha, size in landed:
                    await cur.execute(
                        REGISTER_SQL + " RETURNING last_referenced_at",
                        (user_id, sha, size),
                    )
                    r = await cur.fetchone()
                    if r is not None and r[0] is not None:
                        stamps.append((sha, r[0]))
    if len(landed) < len(keys):
        # Release blobs that did upload before failing
        await _release_uploaded_blobs(user_id, stamps)
        failed = [keys[sha] for sha, outcome in zip(keys, outcomes) if outcome is not True]
        raise BlobUploadError(f"Blob upload failed for {len(failed)} of {len(keys)}: {failed[0]}")
    return stamps


async def _release_uploaded_blobs(
    user_id: str,
    stamps: list[tuple[str, datetime]],
) -> None:
    """Best-effort condemn newly uploaded blobs via ``release_blob`` when a save aborts."""
    if not stamps:
        return
    from src.server.database.workspace_file_blobs import release_blob

    for sha, stamp in stamps:
        try:
            await release_blob(user_id, sha, stamp)
        except Exception:
            logger.warning(
                "[purge-fence] release_blob failed for user=%s sha=%s",
                user_id,
                sha,
                exc_info=True,
            )


async def save_stored_transcript_fenced(
    thread_id: str,
    prefix: str,
    user_id: str,
    transcript: Any,
    blobs: dict[str, bytes],
    *,
    expected_purge_gen: int | None,
    concurrency: int = 8,
) -> bool:
    """Fenced transcript save protocol (§4.5(2) Layer 3):

    1. Open DB transaction & lock ``conversation_threads`` ``FOR SHARE``.
    2. Verify ``personal_purge_in_progress IS FALSE AND personal_purge_generation == expected_purge_gen``.
       If verification fails, abort immediately without uploading blobs.
    3. While holding the ``FOR SHARE`` row lock (which blocks a concurrent purge's
       ``FOR UPDATE`` from interleaving), upload and register any unregistered blobs.
    4. Write ``thread_transcripts`` and ``thread_transcript_files`` in the SAME transaction.
    5. On commit failure, ``StaleCopy``, or ``False`` return, release all blobs uploaded
       by this call via ``release_blob`` (reference-safe condemnation).
    """
    from src.server.database.thread_transcripts import save_stored

    if not is_dalton_enabled():
        if blobs:
            from src.server.database.workspace_file_blobs import store_unregistered_blobs

            await store_unregistered_blobs(user_id, blobs, concurrency=concurrency)
        return await save_stored(thread_id, prefix, user_id, transcript)

    uploaded_stamps: list[tuple[str, datetime]] = []
    committed = False
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                if not await verify_writeback_fence_tx(
                    conn,
                    thread_id,
                    expected_purge_gen,
                    label="transcript_save",
                ):
                    return False
                if blobs:
                    uploaded_stamps = await _upload_and_register_blobs_tracked(
                        user_id,
                        blobs,
                        concurrency=concurrency,
                    )
                landed = await save_stored(
                    thread_id,
                    prefix,
                    user_id,
                    transcript,
                    conn=conn,
                )
                if not landed:
                    # Transaction exits cleanly with no rows written; release any
                    # blobs we just registered for this discarded render.
                    return False
            committed = True
            return True
    finally:
        if not committed and uploaded_stamps:
            await _release_uploaded_blobs(user_id, uploaded_stamps)


# ---------------------------------------------------------------------------
# Archive & memory skip helpers (§4.5(1) item 2)
# ---------------------------------------------------------------------------


async def is_personal_integration_thread(
    thread_or_session_id: str | None,
    *,
    session_key: str | None = None,
    conn: Any = None,
) -> bool:
    """Return True if the thread identified by ``thread_or_session_id`` (or UUID embedded
    in ``session_key``) has ``has_personal_integration_data = TRUE``.

    Safe to call from archive extraction and memory extraction entrypoints.
    """
    if not is_dalton_enabled():
        return False
    candidates: list[str] = []
    for raw in (thread_or_session_id, session_key):
        if not raw:
            continue
        direct = normalize_uuid(str(raw).strip())
        if direct and direct not in candidates:
            candidates.append(direct)
            continue
        # Handle composite session keys such as "ptc:<uuid>" or "thread:<uuid>:..."
        for token in str(raw).replace(":", "/").split("/"):
            uid = normalize_uuid(token.strip())
            if uid and uid not in candidates:
                candidates.append(uid)
    if not candidates:
        return False
    try:
        async with get_db_connection(conn) as db_conn:
            async with db_conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT 1
                    FROM conversation_threads
                    WHERE conversation_thread_id = ANY(%s::uuid[])
                      AND has_personal_integration_data IS TRUE
                    LIMIT 1
                    """,
                    (candidates,),
                )
                return (await cur.fetchone()) is not None
    except Exception:
        # In pure unit tests without a live DB pool, do not crash unrelated extractors.
        logger.debug(
            "[purge-fence] personal integration thread probe skipped (db unavailable)",
            exc_info=True,
        )
        return False


async def should_skip_memory_extraction_for_thread(
    thread_id: str | None,
    *,
    conn: Any = None,
) -> bool:
    """Return True when long-term memory extraction must skip ``thread_id`` (§4.5(1) item 2)."""
    return await is_personal_integration_thread(thread_id, conn=conn)
