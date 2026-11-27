"""Main-bucket consumer reference registry and storage-off startup gate (§6.1 #2).

Shared by Everflow and Standalone Core (``CORE_CAPABILITY = None``).
Batch 1 Step 2 provides:
- Declarative registry of all main-bucket database consumers (aligned with §3.1 matrix)
- Fast ``EXISTS (...) LIMIT 1`` probe queries with per-query ``statement_timeout``
- Startup gate invoked when ``is_storage_enabled()`` is False
- Read-only operator CLI ``python -m src.dalton.storage_refs probe``

Batch 1 Step 3 extends this same module with snapshot reference enumeration and
backup reconciliation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from src.dalton.profile import DaltonStartupConfigError
from src.server.database.pool import get_db_connection
from src.utils.storage import is_storage_enabled

logger = logging.getLogger(__name__)

PROBE_STATEMENT_TIMEOUT = "5s"
ALLOW_UNMIGRATED_ENV = "DALTON_ALLOW_UNMIGRATED_STORAGE_OFF"
STORAGE_OFF_GATE_ERROR_PREFIX = (
    "主桶已有数据，请保持 `STORAGE_PROVIDER=s3`；"
    "只想让新文件写回数据库，请设 `DALTON_WSFILES_WRITE_INLINE_ONLY=1`"
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class MainBucketConsumerProbe:
    """One main-bucket consumer table probe for the storage-off startup gate."""

    name: str
    table: str
    exists_sql: str
    where_summary: str
    source_ref: str


# Every main-bucket consumer from the §3.1 reconciliation matrix.
# Secondary-bucket consumers (dalton_chat_file_uploads, dalton_transcription_jobs,
# channel_snapshots) live in the dedicated secondary bucket and are excluded here.
MAIN_BUCKET_CONSUMER_PROBES: tuple[MainBucketConsumerProbe, ...] = (
    MainBucketConsumerProbe(
        name="workspace_file_blobs",
        table="public.workspace_file_blobs",
        exists_sql="SELECT EXISTS (SELECT 1 FROM workspace_file_blobs LIMIT 1)",
        where_summary="any row",
        source_ref="migrations/versions/039_workspace_file_blobs.py:105; src/server/database/blob_keys.py:158-175",
    ),
    MainBucketConsumerProbe(
        name="workspace_files",
        table="public.workspace_files",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM workspace_files "
            "WHERE blob_sha256 IS NOT NULL OR pack_sha256 IS NOT NULL LIMIT 1)"
        ),
        where_summary="blob_sha256 IS NOT NULL OR pack_sha256 IS NOT NULL",
        source_ref="src/server/database/blob_keys.py:163-168,181",
    ),
    MainBucketConsumerProbe(
        name="thread_transcript_files",
        table="public.thread_transcript_files",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM thread_transcript_files "
            "WHERE content IS NULL LIMIT 1)"
        ),
        where_summary="content IS NULL",
        source_ref="migrations/versions/056_thread_transcripts.py:43-55; src/server/database/blob_keys.py:169-170",
    ),
    MainBucketConsumerProbe(
        name="artifact_version_files",
        table="public.artifact_version_files",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM artifact_version_files "
            "WHERE content IS NULL LIMIT 1)"
        ),
        where_summary="content IS NULL",
        source_ref="migrations/versions/101_artifacts.py:163-175; src/server/database/blob_keys.py:171-172",
    ),
    MainBucketConsumerProbe(
        name="drive_contents",
        table="public.drive_contents",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM drive_contents "
            "WHERE blob_user_id IS NOT NULL LIMIT 1)"
        ),
        where_summary="blob_user_id IS NOT NULL",
        source_ref="migrations/versions/150_drive.py:156-171; src/server/database/blob_keys.py:173-174",
    ),
    MainBucketConsumerProbe(
        name="provenance_result_bodies",
        table="public.provenance_result_bodies",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM provenance_result_bodies "
            "WHERE object_key IS NOT NULL LIMIT 1)"
        ),
        where_summary="object_key IS NOT NULL",
        source_ref="migrations/versions/015_add_provenance_result_bodies.py:38-45; src/server/database/provenance_bodies.py:107-115",
    ),
    MainBucketConsumerProbe(
        name="memo_binaries",
        table="public.store",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM store "
            "WHERE value ? 'binary_ref' "
            "AND jsonb_typeof(value -> 'binary_ref') = 'object' "
            "AND COALESCE(value -> 'binary_ref' ->> 'key', '') <> '' LIMIT 1)"
        ),
        where_summary="value->'binary_ref'->>'key' non-empty",
        source_ref="migrations/versions/001_initial_schema.py:535-544; src/server/app/memo.py:566-575,651; src/server/services/memo_binary_storage.py:125-129",
    ),
    MainBucketConsumerProbe(
        name="user_skills",
        table="public.user_skills",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM user_skills "
            "WHERE archive_key IS NOT NULL LIMIT 1)"
        ),
        where_summary="archive_key IS NOT NULL",
        source_ref="migrations/versions/026_user_skills.py:56-84; src/server/services/skill_archive_storage.py:54-65,94-117",
    ),
    MainBucketConsumerProbe(
        name="org_library_items",
        table="public.org_library_items",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM org_library_items "
            "WHERE archive_key IS NOT NULL LIMIT 1)"
        ),
        where_summary="archive_key IS NOT NULL",
        source_ref="migrations/versions/140_org_library_skills.py:35-54; src/server/services/skill_archive_storage.py:67-91",
    ),
    MainBucketConsumerProbe(
        name="widgets_turn_slices",
        table="public.turn_slices",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM turn_slices "
            "WHERE lines IS NOT NULL AND position('\"data_ref\"' in lines) > 0 LIMIT 1)"
        ),
        where_summary="lines contains \"data_ref\"",
        source_ref="migrations/versions/064_turn_slices.py:27-41; src/server/services/history/replay/lines.py:38-42,55",
    ),
    MainBucketConsumerProbe(
        name="widgets_checkpoint_blobs",
        table="public.checkpoint_blobs",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM checkpoint_blobs "
            "WHERE channel = 'messages' AND blob IS NOT NULL "
            "AND position(convert_to('data_ref', 'UTF8') in blob) > 0 "
            "AND position(convert_to('widgets/', 'UTF8') in blob) > 0 LIMIT 1)"
        ),
        where_summary="channel='messages' blob contains 'data_ref' and 'widgets/'",
        source_ref="src/ptc_agent/agent/tools/show_widget.py:272-305; src/server/services/history/replay/widgets.py:19-39",
    ),
    MainBucketConsumerProbe(
        name="users_avatars",
        table="public.users",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM users "
            "WHERE avatar_url IS NOT NULL "
            "AND avatar_url ~ '(^|/)avatars/[^/]+\\.[A-Za-z0-9]+(\\?|$)' LIMIT 1)"
        ),
        where_summary="avatar_url matches site avatars/<user>.<ext> key",
        source_ref="migrations/versions/001_initial_schema.py:77-81; src/server/app/users.py:1172-1182",
    ),
    MainBucketConsumerProbe(
        name="response_images_turns",
        table="public.conversation_responses",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM conversation_responses "
            "WHERE replay_facts IS NOT NULL "
            "AND replay_facts ? 'images' "
            "AND jsonb_typeof(replay_facts -> 'images') = 'object' "
            "AND replay_facts -> 'images' <> '{}'::jsonb "
            "AND (replay_facts -> 'images')::text LIKE '%response-images/%' LIMIT 1)"
        ),
        where_summary="replay_facts->'images' references response-images/*",
        source_ref="migrations/versions/065_replay_facts.py:25-28; src/server/services/history/replay/facts.py:48-49,181; src/ptc_agent/agent/middleware/image_capture.py:88-91",
    ),
    MainBucketConsumerProbe(
        name="response_images_subagents",
        table="public.subagent_runs",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM subagent_runs "
            "WHERE replay_facts IS NOT NULL "
            "AND replay_facts ? 'images' "
            "AND jsonb_typeof(replay_facts -> 'images') = 'object' "
            "AND replay_facts -> 'images' <> '{}'::jsonb "
            "AND (replay_facts -> 'images')::text LIKE '%response-images/%' LIMIT 1)"
        ),
        where_summary="replay_facts->'images' references response-images/*",
        source_ref="migrations/versions/065_replay_facts.py:29-31; src/server/services/history/replay/facts.py:64-65; src/ptc_agent/agent/middleware/image_capture.py:88-91",
    ),
    MainBucketConsumerProbe(
        name="chat_attachments",
        table="public.chat_attachments",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM chat_attachments "
            "WHERE object_key IS NOT NULL AND cleaned_at IS NULL LIMIT 1)"
        ),
        where_summary="object_key IS NOT NULL AND cleaned_at IS NULL (inline rows excluded)",
        source_ref="migrations/versions/123_chat_attachments.py:49-76; src/server/services/chat_attachments.py:365-379",
    ),
    MainBucketConsumerProbe(
        name="chat_attachment_history",
        table="public.conversation_queries",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM conversation_queries "
            "WHERE metadata IS NOT NULL "
            "AND metadata ? 'attachments' "
            "AND jsonb_typeof(metadata -> 'attachments') = 'array' "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(metadata -> 'attachments') AS att "
            "WHERE COALESCE(att ->> 'url', '') LIKE '%/attachments/%') LIMIT 1)"
        ),
        where_summary="metadata->'attachments' element url contains '/attachments/'",
        source_ref="migrations/versions/001_initial_schema.py:309-325; src/server/utils/multimodal_context.py:171-196; src/server/handlers/chat/ptc_run.py:641-658",
    ),
)


@dataclass(frozen=True)
class ConsumerProbeOutcome:
    """Result of probing one main-bucket consumer."""

    name: str
    table: str
    has_references: bool
    skipped_missing_table: bool = False
    error: str | None = None
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "table": self.table,
            "has_references": self.has_references,
            "skipped_missing_table": self.skipped_missing_table,
            "error": self.error,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }


def _extract_first_value(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    return row[0]


async def _probe_single_consumer(
    conn: Any,
    consumer: MainBucketConsumerProbe,
    *,
    statement_timeout: str,
    target_logger: logging.Logger,
) -> ConsumerProbeOutcome:
    started = time.monotonic()
    try:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (statement_timeout,),
                )
                await cur.execute("SELECT to_regclass(%s)", (consumer.table,))
                reg_row = await cur.fetchone()
                if _extract_first_value(reg_row) is None:
                    elapsed_ms = (time.monotonic() - started) * 1000.0
                    target_logger.info(
                        "Main-storage reference probe skipped %s: table %s does not exist",
                        consumer.name,
                        consumer.table,
                    )
                    return ConsumerProbeOutcome(
                        name=consumer.name,
                        table=consumer.table,
                        has_references=False,
                        skipped_missing_table=True,
                        elapsed_ms=elapsed_ms,
                    )

                await cur.execute(consumer.exists_sql)
                exists_row = await cur.fetchone()
                found = bool(_extract_first_value(exists_row))
                elapsed_ms = (time.monotonic() - started) * 1000.0
                return ConsumerProbeOutcome(
                    name=consumer.name,
                    table=consumer.table,
                    has_references=found,
                    elapsed_ms=elapsed_ms,
                )
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        err_text = f"{type(exc).__name__}: {exc}"
        target_logger.error(
            "Main-storage reference probe failed for %s (%s): %s",
            consumer.name,
            consumer.table,
            err_text,
        )
        return ConsumerProbeOutcome(
            name=consumer.name,
            table=consumer.table,
            has_references=False,
            error=err_text,
            elapsed_ms=elapsed_ms,
        )


async def probe_main_bucket_references(
    *,
    conn: Any | None = None,
    probes: tuple[MainBucketConsumerProbe, ...] = MAIN_BUCKET_CONSUMER_PROBES,
    statement_timeout: str = PROBE_STATEMENT_TIMEOUT,
    target_logger: logging.Logger | None = None,
) -> tuple[list[ConsumerProbeOutcome], float]:
    """Run every main-bucket consumer probe sequentially, each in its own timed transaction."""
    log = target_logger or logger
    total_start = time.monotonic()
    outcomes: list[ConsumerProbeOutcome] = []

    if conn is not None:
        for consumer in probes:
            outcomes.append(
                await _probe_single_consumer(
                    conn,
                    consumer,
                    statement_timeout=statement_timeout,
                    target_logger=log,
                )
            )
    else:
        async with get_db_connection() as db_conn:
            for consumer in probes:
                outcomes.append(
                    await _probe_single_consumer(
                        db_conn,
                        consumer,
                        statement_timeout=statement_timeout,
                        target_logger=log,
                    )
                )

    total_ms = (time.monotonic() - total_start) * 1000.0
    return outcomes, total_ms


def _allow_unmigrated_storage_off() -> bool:
    return (os.getenv(ALLOW_UNMIGRATED_ENV, "") or "").strip().lower() in _TRUTHY


async def enforce_storage_off_startup_gate(
    *,
    conn: Any | None = None,
    probes: tuple[MainBucketConsumerProbe, ...] = MAIN_BUCKET_CONSUMER_PROBES,
    statement_timeout: str = PROBE_STATEMENT_TIMEOUT,
    target_logger: logging.Logger | None = None,
) -> list[ConsumerProbeOutcome]:
    """Refuse startup if main storage is disabled while main-bucket references exist.

    - No-op when ``is_storage_enabled()`` is True.
    - Runs in both Everflow and Standalone Core profiles.
    - Both reference hits and unexpected probe query errors refuse startup
      (unless ``DALTON_ALLOW_UNMIGRATED_STORAGE_OFF=1`` degrades refusal to a
      ``CRITICAL`` log).
    """
    if is_storage_enabled():
        return []

    log = target_logger or logger
    outcomes, total_ms = await probe_main_bucket_references(
        conn=conn,
        probes=probes,
        statement_timeout=statement_timeout,
        target_logger=log,
    )

    hits = [o.name for o in outcomes if o.has_references]
    errors = [f"{o.name} ({o.error})" for o in outcomes if o.error]
    skipped = sum(1 for o in outcomes if o.skipped_missing_table)

    log.info(
        "Main-storage startup reference gate probed %d consumer(s) in %.1f ms "
        "(hits=%d, errors=%d, skipped_missing_tables=%d)",
        len(outcomes),
        total_ms,
        len(hits),
        len(errors),
        skipped,
    )

    if not hits and not errors:
        return outcomes

    details: list[str] = []
    if hits:
        details.append(f"命中的消费者: {', '.join(hits)}")
    if errors:
        details.append(f"探测失败的消费者: {', '.join(errors)}")
    detail_str = "；".join(details)
    message = (
        f"{STORAGE_OFF_GATE_ERROR_PREFIX}（{detail_str}；"
        "修改 .env 后请重建后端容器以加载新环境变量）"
    )

    if _allow_unmigrated_storage_off():
        log.critical(
            "%s — bypassing startup refusal because %s=1 is set",
            message,
            ALLOW_UNMIGRATED_ENV,
        )
        return outcomes

    raise DaltonStartupConfigError(message)


async def _run_cli_probe() -> int:
    outcomes, total_ms = await probe_main_bucket_references()
    payload = {
        "total_elapsed_ms": round(total_ms, 2),
        "hits": [o.name for o in outcomes if o.has_references],
        "errors": [o.name for o in outcomes if o.error],
        "consumers": [o.to_dict() for o in outcomes],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload["errors"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.dalton.storage_refs",
        description=(
            "Read-only main-bucket consumer reference probe (does not print object keys)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "probe",
        help="Check each main-bucket consumer table for existing object references.",
    )
    args = parser.parse_args(argv)
    if args.command == "probe":
        return asyncio.run(_run_cli_probe())
    return 1


if __name__ == "__main__":
    sys.exit(main())
