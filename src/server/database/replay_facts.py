"""The ``replay_facts`` column: what a settled run shows on replay that its
checkpoint does not keep.

Both ledgers carry it. A response row holds its main agent's reasoning
durations (written by the finalize CAS) and, for a turn settled while its live
stream was archived, what that archive added under ``LEGACY_KEY``, or, for one
its checkpoints could not cover, the archive itself under ``ARCHIVE_KEY``
(migration 069). A subagent run's row holds the captured URLs of the images
its lane referenced (``images``) and the steering it handed back unread
(``steering_returned``), written as each is learned, and, when it ran a
model other than its launcher's, that model (``model``, ``{"id",
"provider"}``), written with the row at admission.
"""

from typing import Any, Dict, List, Optional

from psycopg.rows import dict_row

from src.server.database import pool
from src.server.utils.pg_sanitize import SafeJson

LEGACY_KEY = "legacy"
ARCHIVE_KEY = "archive"
# DALTON-HOOK(chat-large-upload): replay_facts key for permanently failed steers
FAILED_STEERS_KEY = "failed_steers"


def archived_stream(
    response: Optional[Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    """The live stream a legacy turn's row kept whole (``ARCHIVE_KEY``), or
    None for a row that kept none. A row keeps it only when its checkpoints
    could not cover its turn, so it is the only record of what the turn
    said. The rows are copies: replay stamps them in place."""
    facts = (response or {}).get("replay_facts")
    rows = facts.get(ARCHIVE_KEY) if isinstance(facts, dict) else None
    if not isinstance(rows, list):
        return None
    return [
        {"event": row["event"], "data": dict(row["data"])}
        for row in rows
        if isinstance(row, dict)
        and row.get("event")
        and isinstance(row.get("data"), dict)
    ]


async def append_returned_steering(
    task_run_id: str, returned: List[Dict[str, Any]], conn=None
) -> Optional[Dict[str, Any]]:
    """Append steering a run handed back, returning its ``thread_id``,
    ``parent_run_id`` and ``replay_facts`` (None when no row matched).

    ``conn`` is the transaction that settled the steers, so a return is
    never recorded for a steer that stayed live. The merge is one statement,
    so an images write landing on the row at the same time keeps its key.
    """
    if not returned:
        return None
    async with pool.get_db_connection(conn) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE subagent_runs
                SET replay_facts = COALESCE(replay_facts, '{}'::jsonb)
                    || jsonb_build_object('steering_returned',
                        COALESCE(replay_facts -> 'steering_returned', '[]'::jsonb)
                        || %(returned)s::jsonb)
                WHERE task_run_id = %(id)s
                RETURNING thread_id, parent_run_id, replay_facts
                """,
                {"id": task_run_id, "returned": SafeJson(returned)},
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def append_lane_images(
    thread_id: str, response_id: str, task_id: str, images: Dict[str, str]
) -> bool:
    """Merge a lane's captured images into every run the turn launched for
    that task; False when it launched none.

    The archive's records are fenced to the turn, so they come from exactly
    these runs, and replay reads a run's facts only through the turn that
    launched it. ``thread_id`` is there for the ``(thread_id, task_id)``
    index, as ``parent_run_id`` has none of its own.
    """
    if not images:
        return False
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE subagent_runs
                SET replay_facts = COALESCE(replay_facts, '{}'::jsonb)
                    || jsonb_build_object('images',
                        COALESCE(replay_facts -> 'images', '{}'::jsonb)
                        || %(images)s::jsonb)
                WHERE thread_id = %(thread)s AND task_id = %(task)s
                  AND parent_run_id = %(response)s
                """,
                {
                    "thread": thread_id,
                    "task": task_id,
                    "response": response_id,
                    "images": SafeJson(images),
                },
            )
            return cur.rowcount > 0


# Every run of a thread that holds replay facts, with the turn index of the
# response that launched it: replay keys a turn's stored lines on the facts of
# the runs it shows, so they are read with the thread's other replay rows.
RUN_FACTS_SQL = """
    SELECT r.turn_index, s.task_run_id, s.replay_facts
    FROM subagent_runs s
    JOIN conversation_responses r
        ON r.conversation_response_id = s.parent_run_id
    WHERE s.thread_id = %s AND s.replay_facts IS NOT NULL
    ORDER BY s.task_run_id
"""


# DALTON-HOOK(chat-large-upload): persist failed steer metadata on conversation_responses.replay_facts
async def append_failed_steer(
    thread_id: str,
    entry: Dict[str, Any],
    *,
    target_run_id: Optional[str] = None,
    conn=None,
) -> Optional[Dict[str, Any]]:
    """Persist a permanently failed steer onto its turn's ``conversation_responses.replay_facts``.

    Written inside the transaction that settles the outbox row so the failed
    steer survives ``purge_terminal_jobs`` without retaining terminal outbox
    rows forever or touching LangGraph checkpoints outside ``WriterGuard``.
    Deduplicated by ``job_id`` and ``request_key`` so retries are idempotent.
    """
    if not thread_id or not entry:
        return None
    job_id = str(entry.get("job_id") or "").strip()
    request_key = str(entry.get("request_key") or "").strip()
    clean_run_id = str(target_run_id or entry.get("failed_run_id") or "").strip()
    async with pool.get_db_connection(conn) as owned:
        async with owned.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                WITH target AS (
                    SELECT conversation_response_id, turn_index
                    FROM conversation_responses
                    WHERE conversation_thread_id = %(thread_id)s::uuid
                    ORDER BY
                        CASE
                            WHEN %(run_id)s <> ''
                             AND conversation_response_id::text = %(run_id)s
                            THEN 0
                            ELSE 1
                        END,
                        turn_index DESC,
                        attempt_no DESC
                    LIMIT 1
                    FOR UPDATE
                )
                UPDATE conversation_responses AS r
                SET replay_facts = jsonb_set(
                    COALESCE(r.replay_facts, '{}'::jsonb),
                    '{failed_steers}',
                    COALESCE(
                        (
                            SELECT jsonb_agg(elem)
                            FROM jsonb_array_elements(
                                COALESCE(r.replay_facts -> 'failed_steers', '[]'::jsonb)
                            ) AS elem
                            WHERE (%(job_id)s = '' OR COALESCE(elem ->> 'job_id', '') <> %(job_id)s)
                              AND (%(request_key)s = '' OR COALESCE(elem ->> 'request_key', '') <> %(request_key)s)
                        ),
                        '[]'::jsonb
                    ) || jsonb_build_array(
                        %(entry)s::jsonb || jsonb_build_object('turn_index', target.turn_index)
                    ),
                    true
                )
                FROM target
                WHERE r.conversation_response_id = target.conversation_response_id
                RETURNING r.conversation_response_id, r.turn_index, r.replay_facts
                """,
                {
                    "thread_id": thread_id,
                    "run_id": clean_run_id,
                    "job_id": job_id,
                    "request_key": request_key,
                    "entry": SafeJson(entry),
                },
            )
            row = await cur.fetchone()
            return dict(row) if row else None
