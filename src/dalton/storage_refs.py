"""Main-bucket consumer reference registry and storage-off startup gate (§6.1 #2).

Shared by Everflow and Standalone Core (``CORE_CAPABILITY = None``).
Batch 1 Step 2 provides:
- Declarative registry of all main-bucket database consumers (aligned with §3.1 matrix)
- Indexed, bounded ``EXISTS (...) LIMIT 1`` probe queries with per-query ``statement_timeout``
- Startup gate invoked when ``is_storage_enabled()`` is False (refuses ONLY on confirmed
  references; timeouts/query errors log a ``WARNING`` and allow startup)
- Read-only operator CLI ``python -m src.dalton.storage_refs probe`` (opens and closes
  its own read-only session connection without requiring the FastAPI lifespan pool)

Batch 1 Step 3 extends this same module with snapshot reference enumeration and
backup reconciliation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from src.dalton.profile import DaltonStartupConfigError
from src.server.database.pool import get_db_connection, open_session_connection
from src.utils.storage import is_storage_enabled

logger = logging.getLogger(__name__)

PROBE_STATEMENT_TIMEOUT = "5s"
ALLOW_UNMIGRATED_ENV = "DALTON_ALLOW_UNMIGRATED_STORAGE_OFF"
STORAGE_OFF_GATE_ERROR_PREFIX = (
    "主桶已有数据，请保持 `STORAGE_PROVIDER=s3`；"
    "只想让新文件写回数据库，请设 `DALTON_WSFILES_WRITE_INLINE_ONLY=1`"
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_ERE_SPECIAL_RE = re.compile(r"([\\.^$*+?()[\]{}|])")
_AVATAR_KEY_TAIL_RE = re.compile(r"^[^/?#]+\.[A-Za-z0-9]+$")


def _escape_ere(literal: str) -> str:
    return _ERE_SPECIAL_RE.sub(r"\\\1", literal)


def _normalize_storage_key_prefix(env: Mapping[str, str] | None = None) -> str:
    em = os.environ if env is None else env
    raw = (em.get("STORAGE_KEY_PREFIX") or "").strip()
    if not raw or raw.startswith("/") or ".." in raw.split("/"):
        return ""
    return raw if raw.endswith("/") else f"{raw}/"


def site_avatar_url_rules(
    *, env: Mapping[str, str] | None = None
) -> list[tuple[str, str, str]]:
    """Return ``(origin, full_avatar_path_prefix, pg_ere_pattern)`` rules for this site.

    Matches ONLY URLs produced by this deployment's main-bucket ``get_public_url("avatars/<id>.<ext>")``:
    1. Origin (case-insensitive ``scheme://host[:port]``) and configured public base path
    2. Configured ``STORAGE_KEY_PREFIX`` (if any)
    3. Single-segment object key ``avatars/<name>.<ext>``

    External avatar URLs (e.g. OAuth/Gravatar/third-party CDNs) or URLs missing the
    configured prefix never match.
    """
    em = os.environ if env is None else env
    candidates: list[str] = []

    explicit_base = (
        em.get("STORAGE_PUBLIC_URL_BASE") or em.get("S3_PUBLIC_URL_BASE") or ""
    ).strip()
    if explicit_base:
        candidates.append(explicit_base)
    else:
        if "STORAGE_BUCKET_NAME" in em:
            s3_bucket = (em.get("STORAGE_BUCKET_NAME") or "").strip()
        else:
            s3_bucket = (em.get("S3_BUCKET_NAME") or "").strip()
        if s3_bucket:
            region = (em.get("STORAGE_REGION") or em.get("S3_REGION") or "us-east-1").strip()
            candidates.append(f"https://{s3_bucket}.s3.{region}.amazonaws.com")

        oss_bucket = (em.get("OSS_BUCKET_NAME") or "").strip()
        oss_endpoint = (em.get("OSS_ENDPOINT") or "").strip()
        if oss_bucket and oss_endpoint:
            candidates.append(f"https://{oss_bucket}.{oss_endpoint}")

        public_app_url = (em.get("PUBLIC_APP_URL") or "").strip()
        if public_app_url:
            candidates.append(f"{public_app_url.rstrip('/')}/storage")

    key_prefix = _normalize_storage_key_prefix(em)
    rules: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_base in candidates:
        parsed = urlsplit(raw_base.rstrip("/"))
        if not parsed.scheme or not parsed.netloc:
            continue
        origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        base_path = parsed.path.rstrip("/")
        full_prefix = f"{base_path}/{key_prefix}avatars/"
        pair = (origin, full_prefix)
        if pair in seen:
            continue
        seen.add(pair)
        path_ere = f"^{_escape_ere(full_prefix)}[^/?#]+\\.[A-Za-z0-9]+([?#].*)?$"
        rules.append((origin, full_prefix, path_ere))
    return rules


def is_site_avatar_url(
    avatar_url: str | None, *, env: Mapping[str, str] | None = None
) -> bool:
    """Return True iff ``avatar_url`` matches this deployment's main-bucket avatar URL."""
    raw = (avatar_url or "").strip()
    if not raw:
        return False
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return False
    origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    path = parsed.path or ""
    for rule_origin, full_prefix, _path_ere in site_avatar_url_rules(env=env):
        if origin != rule_origin:
            continue
        if not path.startswith(full_prefix):
            continue
        tail = path[len(full_prefix) :]
        if _AVATAR_KEY_TAIL_RE.match(tail):
            return True
    return False


def _avatar_probe_params(env: Mapping[str, str] | None = None) -> tuple[list[str], list[str]]:
    rules = site_avatar_url_rules(env=env)
    return [r[0] for r in rules], [r[2] for r in rules]


@dataclass(frozen=True)
class MainBucketConsumerProbe:
    """One main-bucket consumer table probe for the storage-off startup gate."""

    name: str
    table: str
    exists_sql: str
    where_summary: str
    source_ref: str
    index_used: str
    required_tables: tuple[str, ...] = ()
    params_factory: Callable[[Mapping[str, str] | None], tuple[Any, ...]] | None = None

    def tables_to_check(self) -> tuple[str, ...]:
        return self.required_tables or (self.table,)

    def build_params(self, env: Mapping[str, str] | None = None) -> tuple[Any, ...] | None:
        if self.params_factory is None:
            return None
        return self.params_factory(env)


# Every main-bucket consumer from the §3.1 reconciliation matrix.
# Secondary-bucket consumers (dalton_chat_file_uploads, dalton_transcription_jobs,
# channel_snapshots) live in the dedicated secondary bucket and are excluded here.
# Every probe is index-backed with LIMIT 1 so startup checks never full-scan multi-GB tables.
MAIN_BUCKET_CONSUMER_PROBES: tuple[MainBucketConsumerProbe, ...] = (
    MainBucketConsumerProbe(
        name="workspace_file_blobs",
        table="public.workspace_file_blobs",
        exists_sql=(
            "SELECT EXISTS ("
            "SELECT 1 FROM workspace_file_blobs ORDER BY user_id, sha256 LIMIT 1"
            ")"
        ),
        where_summary="any row (PK index-only LIMIT 1)",
        source_ref="migrations/versions/039_workspace_file_blobs.py:105; src/server/database/blob_keys.py:158-175",
        index_used="workspace_file_blobs_pkey (user_id, sha256)",
    ),
    MainBucketConsumerProbe(
        name="workspace_files",
        table="public.workspace_files",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM workspace_files WHERE blob_sha256 IS NOT NULL LIMIT 1) "
            "OR EXISTS (SELECT 1 FROM workspace_files WHERE pack_sha256 IS NOT NULL LIMIT 1)"
        ),
        where_summary="blob_sha256 IS NOT NULL (partial index) OR pack_sha256 IS NOT NULL (partial index)",
        source_ref="migrations/versions/039_workspace_file_blobs.py:172-181; src/server/database/blob_keys.py:163-168,181",
        index_used="idx_workspace_files_blob_sha256 (partial), idx_workspace_files_pack_sha256 (partial)",
    ),
    MainBucketConsumerProbe(
        name="thread_transcript_files",
        table="public.thread_transcript_files",
        exists_sql=(
            "SELECT EXISTS ("
            "SELECT 1 FROM workspace_file_blobs b "
            "JOIN thread_transcript_files t ON t.sha256 = b.sha256 AND t.user_id = b.user_id "
            "WHERE t.content IS NULL LIMIT 1"
            ")"
        ),
        where_summary="blob-backed row (joined via workspace_file_blobs) with content IS NULL",
        source_ref="migrations/versions/056_thread_transcripts.py:43-60; src/server/database/blob_keys.py:169-170",
        index_used="workspace_file_blobs_pkey + idx_thread_transcript_files_sha256",
        required_tables=("public.workspace_file_blobs", "public.thread_transcript_files"),
    ),
    MainBucketConsumerProbe(
        name="artifact_version_files",
        table="public.artifact_version_files",
        exists_sql=(
            "SELECT EXISTS ("
            "SELECT 1 FROM workspace_file_blobs b "
            "JOIN artifact_version_files a ON a.sha256 = b.sha256 AND a.user_id = b.user_id "
            "WHERE a.content IS NULL LIMIT 1"
            ")"
        ),
        where_summary="blob-backed row (joined via workspace_file_blobs) with content IS NULL",
        source_ref="migrations/versions/101_artifacts.py:163-187; src/server/database/blob_keys.py:171-172",
        index_used="workspace_file_blobs_pkey + idx_artifact_version_files_sha256",
        required_tables=("public.workspace_file_blobs", "public.artifact_version_files"),
    ),
    MainBucketConsumerProbe(
        name="drive_contents",
        table="public.drive_contents",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM drive_contents "
            "WHERE blob_user_id IS NOT NULL LIMIT 1)"
        ),
        where_summary="blob_user_id IS NOT NULL",
        source_ref="migrations/versions/150_drive.py:156-184; src/server/database/blob_keys.py:173-174",
        index_used="ix_drive_contents_blob (partial WHERE blob_user_id IS NOT NULL)",
    ),
    MainBucketConsumerProbe(
        name="provenance_result_bodies",
        table="public.provenance_result_bodies",
        exists_sql=(
            "SELECT EXISTS ("
            "SELECT 1 FROM ("
            "SELECT object_key FROM provenance_result_bodies ORDER BY created_at DESC LIMIT 1000"
            ") recent WHERE recent.object_key IS NOT NULL LIMIT 1"
            ")"
        ),
        where_summary="recent 1000 rows by created_at DESC with object_key IS NOT NULL",
        source_ref="migrations/versions/015_add_provenance_result_bodies.py:38-58; src/server/database/provenance_bodies.py:107-115",
        index_used="idx_provenance_result_bodies_created_at (created_at)",
    ),
    MainBucketConsumerProbe(
        name="memo_binaries",
        table="public.store",
        exists_sql=(
            "SELECT EXISTS ("
            "SELECT 1 FROM ("
            "SELECT user_id FROM users ORDER BY created_at DESC LIMIT 1000"
            ") u "
            "JOIN store s ON s.prefix = replace(u.user_id, '.', '%2E') || '.memos' "
            "WHERE s.value ? 'binary_ref' "
            "AND jsonb_typeof(s.value -> 'binary_ref') = 'object' "
            "AND COALESCE(s.value -> 'binary_ref' ->> 'key', '') <> '' LIMIT 1"
            ")"
        ),
        where_summary="users memo namespaces (store_pkey prefix lookup) with non-empty binary_ref.key",
        source_ref="migrations/versions/001_initial_schema.py:535-549; src/ptc_agent/agent/backends/langgraph_store.py:175-176; src/server/app/memo.py:566-575",
        index_used="idx_users_created_at + store_pkey (prefix, key)",
        required_tables=("public.users", "public.store"),
    ),
    MainBucketConsumerProbe(
        name="user_skills",
        table="public.user_skills",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM user_skills "
            "WHERE archive_key IS NOT NULL LIMIT 1)"
        ),
        where_summary="archive_key IS NOT NULL",
        source_ref="migrations/versions/026_user_skills.py:56-120; src/server/services/skill_archive_storage.py:54-65,94-117",
        index_used="idx_user_skills_archive_key (partial WHERE archive_key IS NOT NULL)",
    ),
    MainBucketConsumerProbe(
        name="org_library_items",
        table="public.org_library_items",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM org_library_items "
            "WHERE kind = 'skill' AND archive_key IS NOT NULL LIMIT 1)"
        ),
        where_summary="kind = 'skill' AND archive_key IS NOT NULL",
        source_ref="migrations/versions/140_org_library_skills.py:35-59; src/server/services/skill_archive_storage.py:67-91",
        index_used="idx_org_library_items_skill (partial WHERE kind = 'skill')",
    ),
    MainBucketConsumerProbe(
        name="widgets_turn_slices",
        table="public.turn_slices",
        exists_sql=(
            "SELECT EXISTS ("
            "SELECT 1 FROM ("
            "SELECT conversation_response_id FROM conversation_responses ORDER BY created_at DESC LIMIT 500"
            ") r "
            "JOIN turn_slices ts ON ts.conversation_response_id = r.conversation_response_id "
            "WHERE ts.lines IS NOT NULL AND position('\"data_ref\"' in ts.lines) > 0 LIMIT 1"
            ")"
        ),
        where_summary="recent 500 responses joined to turn_slices PK where lines contains \"data_ref\"",
        source_ref="migrations/versions/064_turn_slices.py:27-47; src/server/services/history/replay/lines.py:38-42,55",
        index_used="idx_responses_created_at + turn_slices_pkey (conversation_response_id)",
        required_tables=("public.conversation_responses", "public.turn_slices"),
    ),
    MainBucketConsumerProbe(
        name="widgets_checkpoint_blobs",
        table="public.checkpoint_blobs",
        exists_sql=(
            "WITH recent_threads AS ("
            "SELECT conversation_thread_id::text AS thread_id FROM ("
            "SELECT conversation_thread_id FROM conversation_responses ORDER BY created_at DESC LIMIT 100"
            ") r "
            "UNION "
            "SELECT conversation_thread_id::text AS thread_id FROM ("
            "SELECT conversation_thread_id FROM conversation_threads ORDER BY created_at DESC LIMIT 100"
            ") t"
            ") "
            "SELECT EXISTS ("
            "SELECT 1 FROM recent_threads rt "
            "JOIN checkpoint_blobs cb ON cb.thread_id = rt.thread_id "
            "WHERE cb.channel = 'messages' AND cb.blob IS NOT NULL "
            "AND position(convert_to('data_ref', 'UTF8') in cb.blob) > 0 "
            "AND position(convert_to('widgets/', 'UTF8') in cb.blob) > 0 LIMIT 1"
            ")"
        ),
        where_summary="recent active threads joined via checkpoint_blobs_thread_id_idx where messages blob contains data_ref + widgets/",
        source_ref="src/ptc_agent/agent/tools/show_widget.py:272-305; src/server/services/history/replay/widgets.py:19-39",
        index_used="idx_responses_created_at, idx_threads_created_at + checkpoint_blobs_thread_id_idx / checkpoint_blobs_pkey",
        required_tables=("public.conversation_responses", "public.conversation_threads", "public.checkpoint_blobs"),
    ),
    MainBucketConsumerProbe(
        name="widgets_checkpoint_writes",
        table="public.checkpoint_writes",
        exists_sql=(
            "WITH recent_threads AS ("
            "SELECT conversation_thread_id::text AS thread_id FROM ("
            "SELECT conversation_thread_id FROM conversation_responses ORDER BY created_at DESC LIMIT 100"
            ") r "
            "UNION "
            "SELECT conversation_thread_id::text AS thread_id FROM ("
            "SELECT conversation_thread_id FROM conversation_threads ORDER BY created_at DESC LIMIT 100"
            ") t"
            ") "
            "SELECT EXISTS ("
            "SELECT 1 FROM recent_threads rt "
            "JOIN checkpoint_writes cw ON cw.thread_id = rt.thread_id "
            "WHERE cw.channel = 'messages' AND cw.blob IS NOT NULL "
            "AND position(convert_to('data_ref', 'UTF8') in cw.blob) > 0 "
            "AND position(convert_to('widgets/', 'UTF8') in cw.blob) > 0 LIMIT 1"
            ")"
        ),
        where_summary="recent active threads joined via checkpoint_writes_thread_id_idx where unsliced DeltaChannel messages blob contains data_ref + widgets/",
        source_ref="src/server/database/thread_fork.py:14-16; src/ptc_agent/agent/tools/show_widget.py:272-305",
        index_used="idx_responses_created_at, idx_threads_created_at + checkpoint_writes_thread_id_idx / checkpoint_writes_pkey",
        required_tables=("public.conversation_responses", "public.conversation_threads", "public.checkpoint_writes"),
    ),
    MainBucketConsumerProbe(
        name="users_avatars",
        table="public.users",
        exists_sql=(
            "SELECT EXISTS ("
            "SELECT 1 FROM ("
            "SELECT avatar_url FROM users ORDER BY created_at DESC LIMIT 1000"
            ") recent "
            "JOIN unnest(%s::text[], %s::text[]) AS cfg(origin, path_re) "
            "ON lower(substring(recent.avatar_url FROM '^[A-Za-z][A-Za-z0-9+.-]*://[^/?#]+')) = cfg.origin "
            "WHERE recent.avatar_url IS NOT NULL "
            "AND substring(recent.avatar_url FROM '^[A-Za-z][A-Za-z0-9+.-]*://[^/?#]+(.*)') ~ cfg.path_re "
            "LIMIT 1"
            ")"
        ),
        where_summary="avatar_url matches site origin + configured STORAGE_KEY_PREFIX + avatars/<user>.<ext> (external URLs allowed)",
        source_ref="migrations/versions/001_initial_schema.py:77-94; src/server/app/users.py:407,1172-1182",
        index_used="idx_users_created_at (created_at DESC)",
        params_factory=_avatar_probe_params,
    ),
    MainBucketConsumerProbe(
        name="response_images_turns",
        table="public.conversation_responses",
        exists_sql=(
            "SELECT EXISTS ("
            "SELECT 1 FROM ("
            "SELECT replay_facts FROM conversation_responses ORDER BY created_at DESC LIMIT 1000"
            ") recent "
            "WHERE recent.replay_facts IS NOT NULL "
            "AND recent.replay_facts ? 'images' "
            "AND jsonb_typeof(recent.replay_facts -> 'images') = 'object' "
            "AND recent.replay_facts -> 'images' <> '{}'::jsonb "
            "AND (recent.replay_facts -> 'images')::text LIKE '%response-images/%' LIMIT 1"
            ")"
        ),
        where_summary="recent 1000 responses by created_at DESC where replay_facts->'images' references response-images/*",
        source_ref="migrations/versions/065_replay_facts.py:25-28; src/server/services/history/replay/facts.py:48-49,181; src/ptc_agent/agent/middleware/image_capture.py:88-91",
        index_used="idx_responses_created_at (created_at DESC)",
    ),
    MainBucketConsumerProbe(
        name="response_images_subagents",
        table="public.subagent_runs",
        exists_sql=(
            "WITH recent_threads AS ("
            "SELECT conversation_thread_id AS thread_id FROM ("
            "SELECT conversation_thread_id FROM conversation_responses ORDER BY created_at DESC LIMIT 500"
            ") r "
            "UNION "
            "SELECT conversation_thread_id AS thread_id FROM ("
            "SELECT conversation_thread_id FROM conversation_threads ORDER BY created_at DESC LIMIT 200"
            ") t"
            ") "
            "SELECT EXISTS ("
            "SELECT 1 FROM recent_threads rt "
            "JOIN subagent_runs s ON s.thread_id = rt.thread_id "
            "WHERE s.replay_facts IS NOT NULL "
            "AND s.replay_facts ? 'images' "
            "AND jsonb_typeof(s.replay_facts -> 'images') = 'object' "
            "AND s.replay_facts -> 'images' <> '{}'::jsonb "
            "AND (s.replay_facts -> 'images')::text LIKE '%response-images/%' LIMIT 1"
            ")"
        ),
        where_summary="recent active threads joined via ix_subagent_runs_thread_task where replay_facts->'images' references response-images/*",
        source_ref="migrations/versions/065_replay_facts.py:29-31; src/server/services/history/replay/facts.py:64-65; src/ptc_agent/agent/middleware/image_capture.py:88-91",
        index_used="idx_responses_created_at, idx_threads_created_at + ix_subagent_runs_thread_task (thread_id, task_id, started_at)",
        required_tables=("public.conversation_responses", "public.conversation_threads", "public.subagent_runs"),
    ),
    MainBucketConsumerProbe(
        name="chat_attachments",
        table="public.chat_attachments",
        exists_sql=(
            "SELECT EXISTS ("
            "SELECT 1 FROM chat_attachments "
            "WHERE sent_at IS NOT NULL AND object_key IS NOT NULL AND cleaned_at IS NULL LIMIT 1"
            ") OR EXISTS ("
            "SELECT 1 FROM chat_attachments "
            "WHERE sent_at IS NULL AND state <> 'aborted' AND object_key IS NOT NULL AND cleaned_at IS NULL LIMIT 1"
            ") OR EXISTS ("
            "SELECT 1 FROM chat_attachments "
            "WHERE state = 'aborted' AND cleaned_at IS NULL AND object_key IS NOT NULL LIMIT 1"
            ")"
        ),
        where_summary="object_key IS NOT NULL AND cleaned_at IS NULL across partial indexes (inline rows excluded)",
        source_ref="migrations/versions/123_chat_attachments.py:49-117; src/server/services/chat_attachments.py:365-379",
        index_used="chat_attachments_sent (partial), chat_attachments_staged (partial), chat_attachments_aborted (partial)",
    ),
    MainBucketConsumerProbe(
        name="chat_attachment_history",
        table="public.conversation_queries",
        exists_sql=(
            "SELECT EXISTS ("
            "SELECT 1 FROM ("
            "SELECT metadata FROM conversation_queries ORDER BY created_at DESC LIMIT 1000"
            ") recent "
            "WHERE recent.metadata IS NOT NULL "
            "AND recent.metadata ? 'attachments' "
            "AND jsonb_typeof(recent.metadata -> 'attachments') = 'array' "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(recent.metadata -> 'attachments') AS att "
            "WHERE COALESCE(att ->> 'url', '') LIKE '%/attachments/%') LIMIT 1"
            ")"
        ),
        where_summary="recent 1000 queries by created_at DESC where metadata->'attachments' element url contains '/attachments/'",
        source_ref="migrations/versions/001_initial_schema.py:309-329; src/server/utils/multimodal_context.py:171-196; src/server/handlers/chat/ptc_run.py:641-658",
        index_used="idx_queries_created_at (created_at DESC)",
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
    env: Mapping[str, str] | None = None,
) -> ConsumerProbeOutcome:
    started = time.monotonic()
    try:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (statement_timeout,),
                )
                for req_table in consumer.tables_to_check():
                    await cur.execute("SELECT to_regclass(%s)", (req_table,))
                    reg_row = await cur.fetchone()
                    if _extract_first_value(reg_row) is None:
                        elapsed_ms = (time.monotonic() - started) * 1000.0
                        target_logger.info(
                            "Main-storage reference probe skipped %s: table %s does not exist",
                            consumer.name,
                            req_table,
                        )
                        return ConsumerProbeOutcome(
                            name=consumer.name,
                            table=consumer.table,
                            has_references=False,
                            skipped_missing_table=True,
                            elapsed_ms=elapsed_ms,
                        )

                params = consumer.build_params(env)
                if params is not None:
                    await cur.execute(consumer.exists_sql, params)
                else:
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
        target_logger.warning(
            "Main-storage reference probe failed or timed out for %s (%s): %s "
            "(not treated as confirmed reference)",
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
    env: Mapping[str, str] | None = None,
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
                    env=env,
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
                        env=env,
                    )
                )

    total_ms = (time.monotonic() - total_start) * 1000.0
    return outcomes, total_ms


async def explain_main_bucket_probes(
    conn: Any,
    *,
    probes: tuple[MainBucketConsumerProbe, ...] = MAIN_BUCKET_CONSUMER_PROBES,
    disable_seqscan: bool = True,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return ``EXPLAIN (FORMAT TEXT)`` plan text per consumer probe (for verification/tests)."""
    plans: dict[str, str] = {}
    for consumer in probes:
        async with conn.transaction():
            async with conn.cursor() as cur:
                if disable_seqscan:
                    await cur.execute("SET LOCAL enable_seqscan = off")
                params = consumer.build_params(env)
                explain_sql = f"EXPLAIN (FORMAT TEXT) {consumer.exists_sql}"
                if params is not None:
                    await cur.execute(explain_sql, params)
                else:
                    await cur.execute(explain_sql)
                rows = await cur.fetchall()
                plans[consumer.name] = "\n".join(str(_extract_first_value(r)) for r in rows)
    return plans


def _allow_unmigrated_storage_off() -> bool:
    return (os.getenv(ALLOW_UNMIGRATED_ENV, "") or "").strip().lower() in _TRUTHY


async def enforce_storage_off_startup_gate(
    *,
    conn: Any | None = None,
    probes: tuple[MainBucketConsumerProbe, ...] = MAIN_BUCKET_CONSUMER_PROBES,
    statement_timeout: str = PROBE_STATEMENT_TIMEOUT,
    target_logger: logging.Logger | None = None,
    env: Mapping[str, str] | None = None,
) -> list[ConsumerProbeOutcome]:
    """Refuse startup ONLY if main storage is disabled AND main-bucket references are confirmed.

    - No-op when ``is_storage_enabled()`` is True.
    - Runs in both Everflow and Standalone Core profiles.
    - Probe timeouts or query errors log a ``WARNING`` and allow startup (never block
      storage-off deployments without a confirmed main-bucket reference).
    - Confirmed reference hits refuse startup (unless
      ``DALTON_ALLOW_UNMIGRATED_STORAGE_OFF=1`` degrades refusal to a ``CRITICAL`` log).
    """
    if is_storage_enabled():
        return []

    log = target_logger or logger
    outcomes, total_ms = await probe_main_bucket_references(
        conn=conn,
        probes=probes,
        statement_timeout=statement_timeout,
        target_logger=log,
        env=env,
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

    if errors and not hits:
        log.warning(
            "Main-storage startup reference gate encountered %d probe timeout/error(s) "
            "with no confirmed main-bucket references; allowing startup: %s",
            len(errors),
            "; ".join(errors),
        )
        return outcomes

    if not hits:
        return outcomes

    details: list[str] = [f"命中的消费者: {', '.join(hits)}"]
    if errors:
        details.append(f"探测超时或出错的消费者: {', '.join(errors)}")
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


async def _open_cli_readonly_connection() -> Any:
    """Open a standalone read-only PostgreSQL session connection for CLI commands."""
    conn = await open_session_connection()
    try:
        async with conn.cursor() as cur:
            await cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    except Exception:
        await conn.close()
        raise
    return conn


async def _run_cli_probe() -> int:
    conn = await _open_cli_readonly_connection()
    try:
        outcomes, total_ms = await probe_main_bucket_references(conn=conn)
    finally:
        await conn.close()
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
