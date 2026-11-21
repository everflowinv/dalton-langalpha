#!/usr/bin/env python3
"""Merge a historical duplicate company workspace into its canonical workspace.

Default mode is a read-only dry run (``default_transaction_read_only = on``) that
runs all preflight checks and prints a detailed plan without mutating the
database or writing an archive.

Scope (narrowed round 2):
- Reassign ``conversation_threads`` from ``dup`` to ``canon``, preserving
  initiator fields (``user_id``, ``created_by``) and appending ``thread_index``
  sequentially.
- Write the latest ``dalton_knowledge_versions`` of each ``doc_key`` from
  ``dup`` onto ``canon`` (v1 when missing on ``canon``, ``latest + 1`` when
  present), using ``canon``'s owner and ``source='server'`` (``d003`` CHECK
  constraint restricts ``source`` to ``tool/direct_edit/import/publish/server``;
  ``merge:<dup_ws>`` is recorded in ``reason`` and ``ops``).
- Upsert ``dalton_workspace_sharing`` for ``canon`` to ``web_access='rw'``,
  ``cos_access='rw'``, ``updated_by=<canon owner>``.
- Mark ``dup.config['dalton_historical_duplicate_of'] = <canon>``.
- Does NOT copy ``workspace_files`` (files remain in the duplicate workspace;
  owners download/upload needed files via the web file panel or read directly
  from the shared duplicate workspace), and does NOT rewrite ``command_runs``,
  ``chart_annotations``, or ``share_links``.

Pass ``--apply`` to:
1. Run ONLY when nobody is actively using ``dup`` or ``canon`` ("在无人使用时运行").
2. Open a single Postgres transaction with ``SET LOCAL lock_timeout = '5s'``.
3. Acquire locks in application order: thread-index advisory locks ->
   ``dalton_workspace_sharing FOR UPDATE`` (matching ``src/dalton/sharing.py:261``) ->
   ``workspaces FOR UPDATE`` -> ``conversation_threads FOR UPDATE`` ->
   knowledge doc advisory locks.
4. Build the merge plan from the locked rows and run all preflight checks
   (refusing sector workspaces, active thread share links, open or
   ``cancelled_in_flight`` wakeups, running or finished-unreported
   ``command_runs``, non-paused automations on ``dup``, and ``pending``/``waiting``/
   ``running`` automation executions).
5. Write and ``fsync`` the archive file (``O_CREAT|O_EXCL|O_WRONLY``, ``0o600``,
   ``ensure_ascii=True``, refusing overwrite) recording every primary key, old
   value, and planned new value (including pre-allocated
   ``knowledge_version_id`` and ``version_no``) BEFORE executing any database
   write; if archive writing fails, roll back.
6. Execute the mutations and assert exact ``rowcount`` after every step.
7. After commit, open a fresh read-only connection to re-check ``is_shared`` on
   every migrated thread and raise a loud alarm if any concurrent share
   enablement occurred.

Pass ``--revert <before.jsonl>`` to reverse an applied merge inside a single
transaction after verifying ``meta.counts`` matches the archive lines, every
modified row still equals its archived new value, ``canon`` has had no new
``conversation_responses`` since merge time, and each inserted knowledge
version is still the latest for its doc with ``files_written_at IS NULL``.

Connection follows the app's own Postgres settings
(``src.server.database.pool.get_db_connection_string()``, i.e. ``DB_*`` env vars
inside the backend container) or an explicit ``--dsn``.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import inspect
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID

import psycopg
from jsonschema import Draft202012Validator
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dalton.coverage.identity import (  # noqa: E402
    is_historical_duplicate_workspace,
    normalize_ticker_key,
)
from src.dalton.knowledge.service import canonical_json  # noqa: E402
from src.dalton.knowledge.validate import get_validator  # noqa: E402
from src.dalton.settings import is_coverage_units_enabled  # noqa: E402
from src.server.database.conversation.threads_write import lock_thread_index  # noqa: E402
from src.server.database.home_workspace import is_home_id, is_home_row  # noqa: E402
from src.server.database.pool import (  # noqa: E402
    _CONNECTION_KWARGS,
    _configure_postgres_connection,
    get_db_connection_string,
)
from src.server.database.runs.lifecycle import workspace_has_live_runs  # noqa: E402
from src.server.database.runs.outbox import (  # noqa: E402
    _ALERT_REVIVALS_SQL,
    _REVIVED_OWED_SQL,
    _REVIVED_TYPES_SQL,
    revive_dead_alerts,
    revive_dead_cleanup_jobs,
)
from src.server.database.wakeups.contract import (  # noqa: E402
    WAKEUP_OWED_SQL,
    jobs_of_sql,
    open_sql,
)
from src.server.database.workspace_names import HOME_FOLDER  # noqa: E402
from src.server.services.report_back.notify_turn import stop_already_covered  # noqa: E402
from src.server.utils.pg_sanitize import (  # noqa: E402
    SafeJson,
    normalize_uuid,
    strip_pg_nul_str,
)

KNOWLEDGE_TRIGGER = "trg_dalton_kv_guard"
LOCK_TIMEOUT = "5s"
POST_COMMIT_RECHECK_TIMEOUT = "10s"
EXIT_POST_COMMIT_RECHECK_FAILED = 3

_REVIVE_ALERTS_SIG = inspect.signature(revive_dead_alerts)
DEAD_ALERT_RETRY_WINDOW_SECONDS = float(
    _REVIVE_ALERTS_SIG.parameters["window_seconds"].default
)
DEAD_ALERT_MAX_REVIVALS = int(
    _REVIVE_ALERTS_SIG.parameters["max_revivals"].default
)
DEAD_CLEANUP_ATTEMPT_CEILING = int(
    inspect.signature(revive_dead_cleanup_jobs).parameters["attempt_ceiling"].default
)
FILE_COPY_NOTICE = (
    "副本工作区的文件留在原处，不随对话迁移。正本所有者可以直接到已共享的副本工作区阅读。"
    "需要把文件放进正本时，不要用网页文件面板上传：上传总是写到工作区根目录，会覆盖同名文件。"
    "请由运维单独处理，放进正本工作区的 cy-anthropic/ 目录（目录名取自副本工作区名的 slug）。"
)
RUN_IDLE_NOTICE = "运行说明：请在无人使用时运行（确保 dup 与 canon 工作区均无并发操作或分享修改）。"
FOLLOW_UP_NOTES = (
    f"  1. {RUN_IDLE_NOTICE}",
    f"  2. {FILE_COPY_NOTICE}",
    "  3. 旧回复的相对文件链接：迁入对话旧回复中不带工作区标识的相对链接（如 research/report.md）仍按对话当前工作区（ANT）解析，上传进 cy-anthropic/ 后可能打开 ANT 同名文件或打不开。",
    "  4. 执行历史按计算机筛选：迁入对话若含原工作区的自动化执行历史，合并后任务仍在原计算机而对话已在 ANT 计算机，按计算机范围筛选执行历史时可能同时查不到这些记录。",
)


class MergeRefusedError(RuntimeError):
    """Raised when preflight, archive, or revert invariants fail."""


def _refuse(message: str) -> None:
    raise MergeRefusedError(f"REFUSED: {message}")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("binary payloads are not expected in before.jsonl archive")
    return str(value)


def _serialize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, UUID):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _timestamps_equal(actual: Any, expected: Any) -> bool:
    if actual is None or expected is None:
        return actual is None and expected is None
    parsed_actual = _parse_ts(actual)
    parsed_expected = _parse_ts(expected)
    if parsed_actual is not None and parsed_expected is not None:
        return parsed_actual == parsed_expected
    return str(actual) == str(expected)


def _default_archive_path(stamp: str) -> Path:
    """Pick a persistent default path for ``before.jsonl`` inside or outside Docker."""
    if Path("/.dockerenv").exists():
        return (
            REPO_ROOT
            / "var"
            / "dalton"
            / "operator-records"
            / f"merge-ws-{stamp}"
            / "before.jsonl"
        )
    return Path.home() / "Backups" / "dalton" / f"merge-ws-{stamp}" / "before.jsonl"


def _parse_config(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    if isinstance(raw, dict):
        return copy.deepcopy(raw)
    return {}


def _configs_equal(actual: Any, expected: Any) -> bool:
    if actual is None or expected is None:
        return actual is None and expected is None
    return _parse_config(actual) == _parse_config(expected)


def _parse_json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _is_home_workspace(row: dict[str, Any]) -> bool:
    user_id = str(row.get("user_id") or "")
    ws_id = str(row.get("workspace_id") or "")
    name = str(row.get("name") or "").strip()
    return bool(
        (user_id and ws_id and is_home_id(user_id, ws_id))
        or is_home_row(row)
        or name.lower() == HOME_FOLDER.lower()
    )


def _thread_has_active_share(thread: dict[str, Any]) -> bool:
    """Return True when the thread currently has an active public share link."""
    is_shared = thread.get("is_shared")
    if is_shared is True:
        return True
    if is_shared is False:
        return False
    return bool(thread.get("share_token"))


def _is_sector_automation(row: dict[str, Any]) -> bool:
    name = str(row.get("name") or "").strip().lower()
    if name.startswith("dalton-sector-"):
        return True
    meta = _parse_json_obj(row.get("metadata"))
    return str(meta.get("subject_kind") or "").strip().lower() == "sector"


def _kv_lock_key(workspace_id: str, doc_key: str) -> str:
    return f"dalton:kv:{str(UUID(str(workspace_id)))}:{doc_key}"


def _build_merge_reason(dup_ws: str, dup_name: str, doc_key: str, dup_version_no: int) -> str:
    return (
        f"merge:{dup_ws}: merged {doc_key} v{dup_version_no} from duplicate "
        f"workspace {dup_ws} ({dup_name})"
    )


def _compute_content_sha256(content: Any, fallback: str = "") -> str:
    if isinstance(content, dict):
        _, sha = canonical_json(content)
        return sha
    return str(fallback or "")


def _store_label(value: str) -> str:
    return value.replace(".", "%2E")


def _workspace_memory_prefix(user_id: str, workspace_id: str) -> str:
    return ".".join((_store_label(user_id), "workspaces", _store_label(workspace_id), "memory"))


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@lru_cache(maxsize=1)
def _company_ticker_validator() -> Draft202012Validator:
    company_schema = get_validator("company").schema
    ticker_schema = dict((company_schema.get("properties") or {}).get("ticker") or {})
    return Draft202012Validator(ticker_schema)


def _matches_company_ticker_schema(candidate: str | None) -> bool:
    """Validate candidate against company.schema.json's ticker property."""
    if not isinstance(candidate, str) or not candidate:
        return False
    if "\n" in candidate or "\r" in candidate:
        return False
    return bool(_company_ticker_validator().is_valid(candidate))


def _resolve_content_ticker_rewrite(
    canon_unit: dict[str, Any], current_content_ticker: str
) -> tuple[str | None, str | None]:
    """Return (rewritten_ticker, explanation_if_skipped) using company.schema.json."""
    unit_ticker = str(canon_unit.get("ticker") or "").strip().upper() or None
    raw_key = str(canon_unit.get("ticker_key") or "").strip().upper() or None
    unit_ticker_key = (
        normalize_ticker_key(raw_key)
        if raw_key
        else (normalize_ticker_key(unit_ticker) if unit_ticker else None)
    )
    if unit_ticker is None and unit_ticker_key is None:
        return None, None
    content_ticker_key = normalize_ticker_key(current_content_ticker)
    same_normalized_key = (
        content_ticker_key is not None
        and unit_ticker_key is not None
        and content_ticker_key == unit_ticker_key
    )
    if unit_ticker is not None and _matches_company_ticker_schema(unit_ticker):
        chosen = unit_ticker
    elif unit_ticker_key is not None and _matches_company_ticker_schema(unit_ticker_key):
        chosen = unit_ticker_key
    else:
        note = (
            f"content['ticker']={current_content_ticker!r} NOT rewritten: "
            f"neither canon_unit.ticker={unit_ticker!r} nor "
            f"ticker_key={unit_ticker_key!r} satisfies company.schema.json"
        )
        return None, note
    # Normalize both sides via ticker_key for comparison, and still rewrite when the
    # raw casing or spelling differs from the schema-compliant target (e.g. 'ant' -> 'ANT').
    if same_normalized_key and current_content_ticker == chosen:
        return None, None
    if current_content_ticker == chosen:
        return None, None
    return chosen, None


def _unfinished_outbox_status_sql(alias: str = "o") -> str:
    """SQL predicate for non-terminal hook_outbox rows per outbox.py."""
    return (
        f"(\n"
        f"    {alias}.status IN ('pending', 'claimed', 'deferred')\n"
        f"    OR (\n"
        f"        {alias}.status = 'dead'\n"
        f"        AND (\n"
        f"            (\n"
        f"                {alias}.hook_type IN {_REVIVED_TYPES_SQL}\n"
        f"                AND COALESCE({alias}.completed_at, {alias}.created_at)\n"
        f"                    > NOW() - make_interval(secs => {DEAD_ALERT_RETRY_WINDOW_SECONDS}::float)\n"
        f"                AND {_ALERT_REVIVALS_SQL} < {DEAD_ALERT_MAX_REVIVALS}\n"
        f"                AND {_REVIVED_OWED_SQL}\n"
        f"            )\n"
        f"            OR (\n"
        f"                {alias}.hook_type = 'watch_clear'\n"
        f"                AND {alias}.attempts < {DEAD_CLEANUP_ATTEMPT_CEILING}\n"
        f"            )\n"
        f"        )\n"
        f"    )\n"
        f")"
    )


@dataclass
class ThreadMoveItem:
    row: dict[str, Any]
    target_workspace_id: str
    target_thread_index: int


@dataclass
class KnowledgeMergeItem:
    doc_key: str
    dup_latest: dict[str, Any]
    canon_latest: dict[str, Any] | None
    target_knowledge_version_id: str
    target_version_no: int
    reason: str
    content_sha256: str
    ticker: str | None
    content: Any | None = None
    original_content_ticker: str | None = None
    ticker_rewrite_note: str | None = None


@dataclass
class MergePlan:
    dup_ws: str
    canon_ws: str
    tx_now: str
    dup_workspace: dict[str, Any]
    canon_workspace: dict[str, Any]
    canon_unit: dict[str, Any]
    canon_max_run_seq: int = 0
    dup_max_run_seq: int = 0
    thread_moves: list[ThreadMoveItem] = field(default_factory=list)
    canon_next_thread_index: int = 0
    open_wakeups: list[dict[str, Any]] = field(default_factory=list)
    running_commands: list[dict[str, Any]] = field(default_factory=list)
    unreported_commands: list[dict[str, Any]] = field(default_factory=list)
    unfinished_outbox_notifications: list[dict[str, Any]] = field(default_factory=list)
    queued_file_comments: list[dict[str, Any]] = field(default_factory=list)
    dup_automations: list[dict[str, Any]] = field(default_factory=list)
    unsettled_automation_executions: list[dict[str, Any]] = field(default_factory=list)
    retained_research_counts: dict[str, int] = field(default_factory=dict)
    knowledge_items: list[KnowledgeMergeItem] = field(default_factory=list)
    invalid_knowledge_versions: list[dict[str, Any]] = field(default_factory=list)
    canon_existing_knowledge: list[dict[str, Any]] = field(default_factory=list)
    dup_sharing: dict[str, Any] | None = None
    canon_sharing: dict[str, Any] | None = None
    dup_new_config: dict[str, Any] = field(default_factory=dict)

    @property
    def threads(self) -> list[dict[str, Any]]:
        return [item.row for item in self.thread_moves]

    def counts(self) -> dict[str, int]:
        return {
            "conversation_threads": len(self.thread_moves),
            "dalton_knowledge_versions": len(self.knowledge_items),
            "dalton_workspace_sharing": 1,
            "workspaces_dup_marked": 1,
        }


class MergeDb:
    """SQL layer for workspace merge preflight, apply, and revert."""

    @asynccontextmanager
    async def connect(
        self, *, dsn: str | None = None, read_only: bool = False
    ) -> AsyncIterator[Any]:
        conninfo = dsn or get_db_connection_string()
        options = "-c default_transaction_read_only=on" if read_only else None
        conn = await psycopg.AsyncConnection.connect(
            conninfo,
            options=options,
            **_CONNECTION_KWARGS,
        )
        try:
            await _configure_postgres_connection(conn)
            yield conn
        finally:
            await conn.close()

    async def set_lock_timeout(self, cur: Any, timeout: str = LOCK_TIMEOUT) -> None:
        if timeout != LOCK_TIMEOUT:
            raise ValueError(f"unsupported lock_timeout literal: {timeout!r}")
        await cur.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")

    async def set_statement_timeout(
        self, cur: Any, timeout: str = POST_COMMIT_RECHECK_TIMEOUT
    ) -> None:
        if timeout != POST_COMMIT_RECHECK_TIMEOUT:
            raise ValueError(f"unsupported statement_timeout literal: {timeout!r}")
        await cur.execute(f"SET statement_timeout = '{POST_COMMIT_RECHECK_TIMEOUT}'")

    async def lock_thread_index(self, cur: Any, workspace_id: str) -> None:
        await lock_thread_index(cur, workspace_id)

    async def lock_workspaces_for_update(
        self, cur: Any, workspace_ids: list[str]
    ) -> None:
        await cur.execute(
            """
            SELECT workspace_id::text AS workspace_id
            FROM workspaces
            WHERE workspace_id = ANY(%s::uuid[])
            ORDER BY workspace_id ASC
            FOR UPDATE
            """,
            (sorted(workspace_ids),),
        )
        await cur.fetchall()

    async def lock_threads_for_update(self, cur: Any, workspace_id: str) -> None:
        await cur.execute(
            """
            SELECT conversation_thread_id::text AS conversation_thread_id
            FROM conversation_threads
            WHERE workspace_id = %s::uuid
            ORDER BY created_at ASC, thread_index ASC, conversation_thread_id ASC
            FOR UPDATE
            """,
            (workspace_id,),
        )
        await cur.fetchall()

    async def lock_threads_by_ids_for_update(
        self, cur: Any, thread_ids: list[str]
    ) -> None:
        if not thread_ids:
            return
        await cur.execute(
            """
            SELECT conversation_thread_id::text AS conversation_thread_id
            FROM conversation_threads
            WHERE conversation_thread_id = ANY(%s::uuid[])
            ORDER BY conversation_thread_id ASC
            FOR UPDATE
            """,
            (sorted(thread_ids),),
        )
        await cur.fetchall()

    async def lock_sharing_for_update(self, cur: Any, workspace_id: str) -> None:
        await cur.execute(
            """
            SELECT workspace_id::text AS workspace_id
            FROM dalton_workspace_sharing
            WHERE workspace_id = %s::uuid
            FOR UPDATE
            """,
            (workspace_id,),
        )
        await cur.fetchall()

    async def lock_knowledge_doc(
        self, cur: Any, workspace_id: str, doc_key: str
    ) -> None:
        await cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (_kv_lock_key(workspace_id, doc_key),),
        )

    async def fetch_knowledge_doc_keys(
        self, cur: Any, workspace_id: str
    ) -> list[str]:
        await cur.execute(
            """
            SELECT DISTINCT doc_key
            FROM dalton_knowledge_versions
            WHERE workspace_id = %s::uuid
            ORDER BY doc_key ASC
            """,
            (workspace_id,),
        )
        return [str(r["doc_key"]) for r in await cur.fetchall()]

    async def fetch_workspace(self, cur: Any, workspace_id: str) -> dict[str, Any] | None:
        await cur.execute(
            """
            SELECT workspace_id::text AS workspace_id, user_id, name, description,
                   sandbox_id, computer_id::text AS computer_id, dir_name,
                   status, config, is_pinned, sort_order,
                   files_restore_incomplete_at, files_restore_offered_at,
                   files_epoch, created_at, updated_at
            FROM workspaces
            WHERE workspace_id = %s::uuid
            """,
            (workspace_id,),
        )
        row = await cur.fetchone()
        return _serialize_row(dict(row)) if row else None

    async def fetch_active_coverage_unit(
        self, cur: Any, workspace_id: str
    ) -> dict[str, Any] | None:
        await cur.execute(
            """
            SELECT unit_id::text AS unit_id, kind, owner_user_id,
                   lead_analyst_user_id, slug, display_name,
                   workspace_id::text AS workspace_id, ticker, ticker_key, status
            FROM dalton_coverage_units
            WHERE workspace_id = %s::uuid AND status = 'active'
            """,
            (workspace_id,),
        )
        row = await cur.fetchone()
        return _serialize_row(dict(row)) if row else None

    async def fetch_live_activity(
        self, cur: Any, workspace_id: str
    ) -> dict[str, Any]:
        has_live_turns_or_subagents = await workspace_has_live_runs(cur, workspace_id)
        await cur.execute(
            """
            SELECT
                (SELECT COUNT(*)::int FROM conversation_responses r
                 JOIN conversation_threads t
                   ON t.conversation_thread_id = r.conversation_thread_id
                 WHERE t.workspace_id = %(ws)s::uuid AND r.status = 'in_progress') AS live_responses,
                (SELECT COUNT(*)::int FROM subagent_runs s
                 JOIN conversation_threads t
                   ON t.conversation_thread_id = s.thread_id
                 WHERE t.workspace_id = %(ws)s::uuid AND s.status = 'in_progress') AS live_subagents,
                (SELECT COUNT(*)::int FROM command_runs c
                 WHERE c.status = 'running'
                   AND (
                       c.workspace_id = %(ws)s::uuid
                       OR c.conversation_thread_id IN (
                           SELECT conversation_thread_id
                           FROM conversation_threads
                           WHERE workspace_id = %(ws)s::uuid
                       )
                   )) AS live_commands
            """,
            {"ws": workspace_id},
        )
        counts = dict(await cur.fetchone() or {})
        return {
            "busy": bool(
                has_live_turns_or_subagents
                or counts.get("live_responses", 0) > 0
                or counts.get("live_subagents", 0) > 0
                or counts.get("live_commands", 0) > 0
            ),
            "has_live_turns_or_subagents": bool(has_live_turns_or_subagents),
            "live_responses": int(counts.get("live_responses", 0)),
            "live_subagents": int(counts.get("live_subagents", 0)),
            "live_commands": int(counts.get("live_commands", 0)),
        }

    async def fetch_threads_for_workspace(
        self, cur: Any, workspace_id: str
    ) -> list[dict[str, Any]]:
        await cur.execute(
            """
            SELECT conversation_thread_id::text AS conversation_thread_id,
                   workspace_id::text AS workspace_id,
                   msg_type, current_status, thread_index, title,
                   external_id, platform, user_id, created_by,
                   share_token, is_shared, share_permissions, shared_at,
                   workspace_shared_at, workspace_shared_by,
                   archived_at, created_at, updated_at
            FROM conversation_threads
            WHERE workspace_id = %s::uuid
            ORDER BY created_at ASC, thread_index ASC, conversation_thread_id ASC
            """,
            (workspace_id,),
        )
        return [_serialize_row(dict(r)) for r in await cur.fetchall()]  # type: ignore[misc]

    async def fetch_threads_by_ids(
        self, cur: Any, thread_ids: list[str]
    ) -> list[dict[str, Any]]:
        if not thread_ids:
            return []
        await cur.execute(
            """
            SELECT conversation_thread_id::text AS conversation_thread_id,
                   workspace_id::text AS workspace_id,
                   thread_index, current_status, title,
                   is_shared, share_token, share_permissions, shared_at,
                   updated_at
            FROM conversation_threads
            WHERE conversation_thread_id = ANY(%s::uuid[])
            ORDER BY conversation_thread_id ASC
            """,
            (sorted(thread_ids),),
        )
        return [_serialize_row(dict(r)) for r in await cur.fetchall()]  # type: ignore[misc]

    async def fetch_next_thread_index(self, cur: Any, workspace_id: str) -> int:
        await cur.execute(
            "SELECT COALESCE(MAX(thread_index), -1) + 1 AS next_idx "
            "FROM conversation_threads WHERE workspace_id = %s::uuid",
            (workspace_id,),
        )
        row = await cur.fetchone()
        return int((row or {}).get("next_idx", 0))

    async def fetch_open_wakeups_for_workspace(
        self, cur: Any, workspace_id: str
    ) -> list[dict[str, Any]]:
        await cur.execute(
            f"""
            SELECT w.wakeup_id::text AS wakeup_id,
                   w.short_id,
                   w.conversation_thread_id::text AS conversation_thread_id,
                   w.user_id,
                   w.label,
                   w.kind,
                   w.status,
                   w.cancelled_by,
                   w.cancelled_at,
                   w.next_fire_at,
                   w.last_fired_seq,
                   w.delivered_seq,
                   w.expires_at,
                   w.created_at,
                   w.updated_at
            FROM wakeups w
            JOIN conversation_threads t
              ON t.conversation_thread_id = w.conversation_thread_id
            WHERE t.workspace_id = %s::uuid
              AND (
                  {open_sql("w")}
                  OR EXISTS (
                      SELECT 1
                      FROM hook_outbox o
                      WHERE {jobs_of_sql("o", "w")}
                        AND (
                            o.status IN ('pending', 'claimed', 'deferred')
                            OR (
                                o.status = 'dead'
                                AND o.hook_type IN {_REVIVED_TYPES_SQL}
                                AND COALESCE(o.completed_at, o.created_at)
                                    > NOW() - make_interval(secs => {DEAD_ALERT_RETRY_WINDOW_SECONDS}::float)
                                AND {_ALERT_REVIVALS_SQL} < {DEAD_ALERT_MAX_REVIVALS}
                            )
                        )
                        AND {WAKEUP_OWED_SQL}
                  )
              )
            ORDER BY w.created_at ASC, w.wakeup_id ASC
            """,
            (workspace_id,),
        )
        return [_serialize_row(dict(r)) for r in await cur.fetchall()]  # type: ignore[misc]

    async def fetch_running_command_runs_for_workspace(
        self, cur: Any, workspace_id: str
    ) -> list[dict[str, Any]]:
        await cur.execute(
            """
            SELECT c.command_id,
                   c.conversation_thread_id::text AS conversation_thread_id,
                   c.user_id,
                   c.workspace_id::text AS workspace_id,
                   c.computer_id::text AS computer_id,
                   c.tool,
                   c.description,
                   c.status,
                   c.started_at,
                   c.updated_at
            FROM command_runs c
            WHERE c.status = 'running'
              AND (
                  c.workspace_id = %s::uuid
                  OR c.conversation_thread_id IN (
                      SELECT conversation_thread_id
                      FROM conversation_threads
                      WHERE workspace_id = %s::uuid
                  )
              )
            ORDER BY c.started_at ASC, c.command_id ASC
            """,
            (workspace_id, workspace_id),
        )
        return [_serialize_row(dict(r)) for r in await cur.fetchall()]  # type: ignore[misc]

    async def fetch_unreported_command_runs_for_workspace(
        self, cur: Any, workspace_id: str
    ) -> list[dict[str, Any]]:
        """Finished command_runs whose report-back/notice is still owed per ``report_back/command.py``."""
        await cur.execute(
            f"""
            SELECT c.command_id,
                   c.conversation_thread_id::text AS conversation_thread_id,
                   c.user_id,
                   c.workspace_id::text AS workspace_id,
                   c.computer_id::text AS computer_id,
                   c.run_id::text AS run_id,
                   c.tool,
                   c.description,
                   c.status,
                   c.failure_reason,
                   c.stopped_by,
                   c.started_at,
                   c.ended_at,
                   c.result_delivered_at,
                   c.updated_at,
                   o.hook_outbox_id::text AS outbox_id,
                   o.hook_type AS outbox_hook_type,
                   o.status AS outbox_status,
                   pr.status AS parent_status,
                   pr.metadata AS parent_metadata,
                   pr.cancel_requested_at AS parent_cancel_requested_at
            FROM command_runs c
            JOIN conversation_threads t
              ON t.conversation_thread_id = c.conversation_thread_id
            LEFT JOIN workspaces w
              ON w.workspace_id = t.workspace_id
            LEFT JOIN conversation_responses pr
              ON pr.conversation_response_id = c.run_id
            JOIN hook_outbox o
              ON o.idempotency_key = 'command:' || c.command_id || ':report_back'
            WHERE (
                c.workspace_id = %s::uuid
                OR t.workspace_id = %s::uuid
            )
              AND (
                  c.status IN ('completed', 'failed')
                  OR (c.status = 'stopped' AND c.stopped_by = 'user')
              )
              AND c.result_delivered_at IS NULL
              AND t.archived_at IS NULL
              AND (w.status IS NULL OR w.status <> 'deleted')
              AND (
                  o.status IN ('pending', 'claimed', 'deferred')
                  OR (
                      o.status = 'dead'
                      AND o.hook_type = 'command_report_back'
                      AND COALESCE(o.completed_at, o.created_at)
                          > NOW() - make_interval(secs => {DEAD_ALERT_RETRY_WINDOW_SECONDS}::float)
                      AND {_ALERT_REVIVALS_SQL} < {DEAD_ALERT_MAX_REVIVALS}
                      AND {_REVIVED_OWED_SQL}
                  )
              )
            ORDER BY c.ended_at ASC NULLS LAST, c.started_at ASC, c.command_id ASC
            """,
            (workspace_id, workspace_id),
        )
        rows = [_serialize_row(dict(r)) for r in await cur.fetchall()]  # type: ignore[misc]
        owed: list[dict[str, Any]] = []
        for row in rows:
            if row is None:
                continue
            if row.get("run_id"):
                parent = {
                    "status": row.get("parent_status"),
                    "metadata": (
                        _parse_json_obj(row.get("parent_metadata"))
                        if row.get("parent_metadata") is not None
                        else None
                    ),
                    "cancel_requested_at": _parse_ts(row.get("parent_cancel_requested_at")),
                }
                if stop_already_covered(parent, _parse_ts(row.get("ended_at"))):
                    continue
            owed.append(row)
        return owed

    async def fetch_unfinished_outbox_notifications_for_workspace(
        self, cur: Any, workspace_id: str
    ) -> list[dict[str, Any]]:
        """Return every non-terminal hook_outbox row targeting dup threads/runs."""
        await cur.execute(
            f"""
            WITH dup_threads AS (
                SELECT conversation_thread_id
                FROM conversation_threads
                WHERE workspace_id = %(ws)s::uuid
            ),
            dup_runs AS (
                SELECT r.conversation_response_id
                FROM conversation_responses r
                JOIN dup_threads dt
                  ON dt.conversation_thread_id = r.conversation_thread_id
            ),
            dup_task_runs AS (
                SELECT s.task_run_id
                FROM subagent_runs s
                JOIN dup_threads dt
                  ON dt.conversation_thread_id = s.thread_id
            ),
            dup_commands AS (
                SELECT c.command_id
                FROM command_runs c
                LEFT JOIN dup_threads dt
                  ON dt.conversation_thread_id = c.conversation_thread_id
                WHERE c.workspace_id = %(ws)s::uuid
                   OR dt.conversation_thread_id IS NOT NULL
            )
            SELECT o.hook_outbox_id::text AS hook_outbox_id,
                   o.run_id::text AS run_id,
                   o.conversation_thread_id::text AS conversation_thread_id,
                   o.hook_type,
                   o.status,
                   o.attempts,
                   o.ordering_key,
                   o.idempotency_key,
                   o.payload,
                   o.next_retry_at::text AS next_retry_at,
                   (o.next_retry_at = 'infinity'::timestamptz) AS is_deferred,
                   o.lease_expires_at,
                   o.created_at,
                   o.completed_at
            FROM hook_outbox o
            WHERE {_unfinished_outbox_status_sql("o")}
              AND (
                  o.conversation_thread_id IN (SELECT conversation_thread_id FROM dup_threads)
                  OR o.run_id IN (SELECT conversation_response_id FROM dup_runs)
                  OR o.ordering_key IN (SELECT conversation_thread_id::text FROM dup_threads)
                  OR (o.payload->>'thread_id') IN (SELECT conversation_thread_id::text FROM dup_threads)
                  OR (o.payload->>'ptc_thread_id') IN (SELECT conversation_thread_id::text FROM dup_threads)
                  OR (o.payload->>'conversation_thread_id') IN (SELECT conversation_thread_id::text FROM dup_threads)
                  OR (o.payload->>'origin_home_thread_id') IN (SELECT conversation_thread_id::text FROM dup_threads)
                  OR (o.payload->'origin'->>'origin_home_thread_id') IN (SELECT conversation_thread_id::text FROM dup_threads)
                  OR (o.payload->>'run_id') IN (SELECT conversation_response_id::text FROM dup_runs)
                  OR (o.payload->>'dispatched_run_id') IN (SELECT conversation_response_id::text FROM dup_runs)
                  OR (o.payload->>'steered_run_id') IN (SELECT conversation_response_id::text FROM dup_runs)
                  OR (o.payload->>'task_run_id') IN (SELECT task_run_id::text FROM dup_task_runs)
                  OR (o.payload->>'steered_run_id') IN (SELECT task_run_id::text FROM dup_task_runs)
                  OR (o.payload->>'command_id') IN (SELECT command_id FROM dup_commands)
              )
            ORDER BY o.hook_type ASC, o.created_at ASC, o.hook_outbox_id ASC
            """,
            {"ws": workspace_id},
        )
        return [_serialize_row(dict(r)) for r in await cur.fetchall()]  # type: ignore[misc]

    async def fetch_queued_file_comments_for_workspace(
        self, cur: Any, workspace_id: str
    ) -> list[dict[str, Any]]:
        """Return queued artifact comments on ``workspace_id`` threads referencing files in ``workspace_id``."""
        await cur.execute(
            """
            SELECT c.comment_id::text AS comment_id,
                   c.artifact_id::text AS artifact_id,
                   c.number,
                   c.path,
                   c.title,
                   c.status,
                   c.delivery,
                   c.user_id,
                   c.thread_id::text AS thread_id,
                   l.workspace_id::text AS artifact_workspace_id,
                   t.title AS thread_title
            FROM artifact_comments c
            JOIN share_links l
              ON l.artifact_id = c.artifact_id
            JOIN conversation_threads t
              ON t.conversation_thread_id = c.thread_id
            WHERE t.workspace_id = %s::uuid
              AND l.workspace_id = %s::uuid
              AND c.delivery = 'queued'
            ORDER BY l.created_at ASC, l.artifact_id ASC, c.number ASC
            """,
            (workspace_id, workspace_id),
        )
        return [_serialize_row(dict(r)) for r in await cur.fetchall()]  # type: ignore[misc]

    async def fetch_db_watermark(
        self, cur: Any, canon_ws: str, dup_ws: str
    ) -> dict[str, Any]:
        """Fetch Postgres ``NOW()`` and current max ``conversation_responses.run_seq``."""
        await cur.execute(
            """
            SELECT NOW() AS db_now,
                   COALESCE(
                       (
                           SELECT MAX(r.run_seq)
                           FROM conversation_responses r
                           JOIN conversation_threads t
                             ON t.conversation_thread_id = r.conversation_thread_id
                           WHERE t.workspace_id = %s::uuid
                       ),
                       0
                   )::bigint AS canon_max_run_seq,
                   COALESCE(
                       (
                           SELECT MAX(r.run_seq)
                           FROM conversation_responses r
                           JOIN conversation_threads t
                             ON t.conversation_thread_id = r.conversation_thread_id
                           WHERE t.workspace_id = %s::uuid
                       ),
                       0
                   )::bigint AS dup_max_run_seq
            """,
            (canon_ws, dup_ws),
        )
        row = dict(await cur.fetchone() or {})
        serialized = _serialize_row(row) or {}
        return {
            "db_now": serialized.get("db_now"),
            "canon_max_run_seq": int(serialized.get("canon_max_run_seq") or 0),
            "dup_max_run_seq": int(serialized.get("dup_max_run_seq") or 0),
        }

    async def fetch_unsettled_automation_executions(
        self,
        cur: Any,
        automation_ids: list[str],
        thread_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Return pending/waiting/running executions for dup automations or threads."""
        aids = sorted({str(a) for a in automation_ids if a})
        tids = sorted({str(t) for t in thread_ids if t})
        if not aids and not tids:
            return []
        await cur.execute(
            """
            SELECT e.automation_execution_id::text AS automation_execution_id,
                   e.automation_id::text AS automation_id,
                   e.status,
                   e.conversation_thread_id::text AS conversation_thread_id,
                   e.scheduled_at,
                   e.started_at,
                   e.created_at,
                   a.name AS automation_name,
                   a.workspace_id::text AS automation_workspace_id,
                   a.status AS automation_status
            FROM automation_executions e
            JOIN automations a ON a.automation_id = e.automation_id
            WHERE e.status IN ('pending', 'waiting', 'running')
              AND (
                  e.automation_id = ANY(%s::uuid[])
                  OR e.conversation_thread_id = ANY(%s::uuid[])
              )
            ORDER BY e.created_at ASC, e.automation_execution_id ASC
            """,
            (aids, tids),
        )
        return [_serialize_row(dict(r)) for r in await cur.fetchall()]  # type: ignore[misc]

    async def fetch_dup_retained_research_counts(
        self, cur: Any, dup_ws: str, dup_user_id: str
    ) -> dict[str, int]:
        """Count research/memory rows that remain on ``dup`` (not moved to ``canon``)."""
        mem_prefix = _workspace_memory_prefix(dup_user_id, dup_ws) if dup_user_id else ""
        mem_like = f"{_escape_like(mem_prefix)}.%" if mem_prefix else ""
        await cur.execute(
            """
            SELECT
                (SELECT COUNT(*)::int FROM dalton_verdicts
                 WHERE workspace_id = %(ws)s::uuid) AS dalton_verdicts,
                (SELECT COUNT(*)::int FROM dalton_signals
                 WHERE workspace_id = %(ws)s::uuid) AS dalton_signals,
                (SELECT COUNT(*)::int FROM dalton_publications
                 WHERE workspace_id = %(ws)s::uuid) AS dalton_publications,
                (SELECT COUNT(*)::int FROM dalton_publish_requests
                 WHERE workspace_id = %(ws)s::uuid) AS dalton_publish_requests,
                (CASE WHEN %(mem_prefix)s = '' THEN 0
                      ELSE (SELECT COUNT(*)::int FROM store
                            WHERE prefix = %(mem_prefix)s
                               OR prefix LIKE %(mem_like)s ESCAPE '\\')
                 END) AS workspace_memory
            """,
            {"ws": dup_ws, "mem_prefix": mem_prefix, "mem_like": mem_like},
        )
        row = dict(await cur.fetchone() or {})
        return {
            "dalton_verdicts": int(row.get("dalton_verdicts", 0)),
            "dalton_signals": int(row.get("dalton_signals", 0)),
            "dalton_publications": int(row.get("dalton_publications", 0)),
            "dalton_publish_requests": int(row.get("dalton_publish_requests", 0)),
            "workspace_memory": int(row.get("workspace_memory", 0)),
        }

    async def fetch_canon_responses_after(
        self,
        cur: Any,
        canon_ws: str,
        *,
        since_ts: str | None = None,
        canon_max_run_seq: int | None = None,
        dup_max_run_seq: int | None = None,
        migrated_thread_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return ``conversation_responses`` on ``canon_ws`` newer than recorded watermarks."""
        migrated_ids = sorted({str(tid) for tid in (migrated_thread_ids or []) if tid})
        if canon_max_run_seq is not None:
            effective_dup_seq = (
                int(dup_max_run_seq)
                if dup_max_run_seq is not None
                else int(canon_max_run_seq)
            )
            await cur.execute(
                """
                SELECT r.conversation_response_id::text AS conversation_response_id,
                       r.conversation_thread_id::text AS conversation_thread_id,
                       r.turn_index,
                       r.run_seq,
                       r.status,
                       r.created_at
                FROM conversation_responses r
                JOIN conversation_threads t
                  ON t.conversation_thread_id = r.conversation_thread_id
                WHERE t.workspace_id = %(canon_ws)s::uuid
                  AND (
                      (
                          NOT (r.conversation_thread_id = ANY(%(migrated_ids)s::uuid[]))
                          AND (
                              r.run_seq > %(canon_max_run_seq)s
                              OR (
                                  %(since_ts)s::timestamptz IS NOT NULL
                                  AND r.created_at > %(since_ts)s::timestamptz
                              )
                          )
                      )
                      OR (
                          r.conversation_thread_id = ANY(%(migrated_ids)s::uuid[])
                          AND (
                              r.run_seq > %(dup_max_run_seq)s
                              OR (
                                  %(since_ts)s::timestamptz IS NOT NULL
                                  AND r.created_at > %(since_ts)s::timestamptz
                              )
                          )
                      )
                  )
                ORDER BY r.run_seq ASC, r.created_at ASC, r.conversation_response_id ASC
                """,
                {
                    "canon_ws": canon_ws,
                    "since_ts": since_ts,
                    "canon_max_run_seq": int(canon_max_run_seq),
                    "dup_max_run_seq": effective_dup_seq,
                    "migrated_ids": migrated_ids,
                },
            )
        else:
            await cur.execute(
                """
                SELECT r.conversation_response_id::text AS conversation_response_id,
                       r.conversation_thread_id::text AS conversation_thread_id,
                       r.turn_index,
                       r.run_seq,
                       r.status,
                       r.created_at
                FROM conversation_responses r
                JOIN conversation_threads t
                  ON t.conversation_thread_id = r.conversation_thread_id
                WHERE t.workspace_id = %s::uuid
                  AND r.created_at > %s::timestamptz
                ORDER BY r.run_seq ASC, r.created_at ASC, r.conversation_response_id ASC
                """,
                (canon_ws, since_ts),
            )
        return [_serialize_row(dict(r)) for r in await cur.fetchall()]  # type: ignore[misc]

    async def fetch_automations_for_workspace(
        self,
        cur: Any,
        workspace_id: str,
        thread_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        ids = list(thread_ids or [])
        if ids:
            await cur.execute(
                """
                SELECT automation_id::text AS automation_id,
                       name,
                       file_name,
                       workspace_id::text AS workspace_id,
                       status,
                       thread_strategy,
                       conversation_thread_id::text AS conversation_thread_id,
                       metadata,
                       created_at,
                       updated_at
                FROM automations
                WHERE deleted_at IS NULL
                  AND (
                      workspace_id = %s::uuid
                      OR conversation_thread_id = ANY(%s::uuid[])
                  )
                ORDER BY created_at ASC, automation_id ASC
                """,
                (workspace_id, ids),
            )
        else:
            await cur.execute(
                """
                SELECT automation_id::text AS automation_id,
                       name,
                       file_name,
                       workspace_id::text AS workspace_id,
                       status,
                       thread_strategy,
                       conversation_thread_id::text AS conversation_thread_id,
                       metadata,
                       created_at,
                       updated_at
                FROM automations
                WHERE deleted_at IS NULL
                  AND workspace_id = %s::uuid
                ORDER BY created_at ASC, automation_id ASC
                """,
                (workspace_id,),
            )
        return [_serialize_row(dict(r)) for r in await cur.fetchall()]  # type: ignore[misc]

    async def fetch_latest_knowledge_versions(
        self, cur: Any, workspace_id: str
    ) -> dict[str, dict[str, Any]]:
        await cur.execute(
            """
            SELECT DISTINCT ON (doc_key)
                   knowledge_version_id::text AS knowledge_version_id,
                   user_id, workspace_id::text AS workspace_id, ticker,
                   doc_key, version_no, source, content, content_sha256,
                   ops, reason, evidence_refs, warnings, schema_valid,
                   edited_path, conversation_thread_id::text AS conversation_thread_id,
                   run_id::text AS run_id, tool_call_id, files_written_at, created_at
            FROM dalton_knowledge_versions
            WHERE workspace_id = %s::uuid
            ORDER BY doc_key ASC, version_no DESC
            """,
            (workspace_id,),
        )
        rows = [_serialize_row(dict(r)) for r in await cur.fetchall()]
        return {str(r["doc_key"]): r for r in rows if r is not None}

    async def fetch_all_knowledge_versions(
        self, cur: Any, workspace_id: str
    ) -> list[dict[str, Any]]:
        await cur.execute(
            """
            SELECT knowledge_version_id::text AS knowledge_version_id,
                   user_id, workspace_id::text AS workspace_id, ticker,
                   doc_key, version_no, source, content, content_sha256,
                   ops, reason, evidence_refs, warnings, schema_valid,
                   edited_path, conversation_thread_id::text AS conversation_thread_id,
                   run_id::text AS run_id, tool_call_id, files_written_at, created_at
            FROM dalton_knowledge_versions
            WHERE workspace_id = %s::uuid
            ORDER BY doc_key ASC, version_no ASC
            """,
            (workspace_id,),
        )
        return [_serialize_row(dict(r)) for r in await cur.fetchall()]  # type: ignore[misc]

    async def fetch_sharing_row(
        self, cur: Any, workspace_id: str
    ) -> dict[str, Any] | None:
        await cur.execute(
            """
            SELECT workspace_id::text AS workspace_id,
                   cos_access, web_access, updated_by, updated_at
            FROM dalton_workspace_sharing
            WHERE workspace_id = %s::uuid
            """,
            (workspace_id,),
        )
        row = await cur.fetchone()
        return _serialize_row(dict(row)) if row else None

    async def move_threads(
        self,
        cur: Any,
        dup_ws: str,
        canon_ws: str,
        thread_moves: list[ThreadMoveItem],
    ) -> int:
        if not thread_moves:
            return 0
        thread_ids = [str(m.row["conversation_thread_id"]) for m in thread_moves]
        target_indices = [int(m.target_thread_index) for m in thread_moves]
        await cur.execute(
            """
            WITH planned AS (
                SELECT * FROM UNNEST(%(thread_ids)s::uuid[], %(indices)s::int[])
                AS p(conversation_thread_id, thread_index)
            )
            UPDATE conversation_threads t
            SET workspace_id = %(canon)s::uuid,
                thread_index = planned.thread_index
            FROM planned
            WHERE t.conversation_thread_id = planned.conversation_thread_id
              AND t.workspace_id = %(dup)s::uuid
            """,
            {
                "canon": canon_ws,
                "dup": dup_ws,
                "thread_ids": thread_ids,
                "indices": target_indices,
            },
        )
        return int(cur.rowcount)

    async def insert_merged_knowledge_version(
        self,
        cur: Any,
        *,
        canon_user_id: str,
        canon_ws: str,
        dup_ws: str,
        item: KnowledgeMergeItem,
    ) -> dict[str, Any]:
        """Insert a merged knowledge version using the exact planned ID and version."""
        src = item.dup_latest
        await self.lock_knowledge_doc(cur, canon_ws, item.doc_key)
        await cur.execute(
            """
            SELECT version_no
            FROM dalton_knowledge_versions
            WHERE workspace_id = %s::uuid AND doc_key = %s
            ORDER BY version_no DESC
            LIMIT 1
            """,
            (canon_ws, item.doc_key),
        )
        latest_row = await cur.fetchone()
        latest_no = int(latest_row["version_no"]) if latest_row else None
        next_no = (latest_no or 0) + 1
        if next_no != item.target_version_no:
            raise RuntimeError(
                f"knowledge version drift for {item.doc_key}: "
                f"expected v{item.target_version_no}, computed v{next_no}"
            )
        ops_payload = [
            {
                "op": "merge",
                "source": f"merge:{dup_ws}",
                "from_workspace_id": dup_ws,
                "from_version_no": int(src["version_no"]),
                "from_knowledge_version_id": str(src["knowledge_version_id"]),
            }
        ]
        # d003_knowledge.py enforces CHECK (source IN ('tool','direct_edit','import','publish','server')).
        # Use 'server' for the CHECK column and carry 'merge:<dup_ws>' in reason + ops.
        payload_content = item.content if item.content is not None else src["content"]
        await cur.execute(
            """
            INSERT INTO dalton_knowledge_versions (
                knowledge_version_id, user_id, workspace_id, doc_key, version_no, source,
                content, content_sha256, ops, reason, evidence_refs,
                warnings, schema_valid, edited_path,
                conversation_thread_id, run_id, tool_call_id, ticker
            ) VALUES (
                %s::uuid, %s, %s::uuid, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s
            )
            RETURNING knowledge_version_id::text AS knowledge_version_id,
                      doc_key, version_no, source, ticker
            """,
            (
                item.target_knowledge_version_id,
                strip_pg_nul_str(canon_user_id),
                canon_ws,
                strip_pg_nul_str(item.doc_key),
                next_no,
                "server",
                SafeJson(payload_content),
                strip_pg_nul_str(item.content_sha256),
                SafeJson(ops_payload),
                strip_pg_nul_str(item.reason),
                SafeJson(src.get("evidence_refs") or []),
                SafeJson(src.get("warnings") or []),
                bool(src.get("schema_valid", True)),
                None,
                None,
                None,
                None,
                strip_pg_nul_str(item.ticker) if item.ticker else None,
            ),
        )
        inserted = await cur.fetchone()
        if cur.rowcount != 1 or inserted is None:
            raise RuntimeError(
                f"insert_merged_knowledge_version rowcount={cur.rowcount} for {item.doc_key}"
            )
        if (
            str(inserted["knowledge_version_id"]) != item.target_knowledge_version_id
            or int(inserted["version_no"]) != item.target_version_no
        ):
            raise RuntimeError(
                f"inserted knowledge row mismatch for {item.doc_key}: "
                f"expected ({item.target_knowledge_version_id}, v{item.target_version_no}), "
                f"got ({inserted['knowledge_version_id']}, v{inserted['version_no']})"
            )
        if (
            item.doc_key == "sector"
            and bool(src.get("schema_valid", True))
            and is_coverage_units_enabled()
        ):
            from src.dalton.database.sector_members import sync_members_projection

            await sync_members_projection(cur, canon_ws, payload_content, next_no)
        return dict(inserted)

    async def upsert_canon_sharing(
        self,
        cur: Any,
        canon_ws: str,
        updated_by: str,
        *,
        updated_at: str,
    ) -> int:
        await cur.execute(
            """
            INSERT INTO dalton_workspace_sharing (
                workspace_id, cos_access, web_access, updated_by, updated_at
            )
            VALUES (%s::uuid, 'rw', 'rw', %s, %s::timestamptz)
            ON CONFLICT (workspace_id) DO UPDATE SET
                cos_access = 'rw',
                web_access = 'rw',
                updated_by = EXCLUDED.updated_by,
                updated_at = EXCLUDED.updated_at
            """,
            (canon_ws, updated_by, updated_at),
        )
        return int(cur.rowcount)

    async def mark_dup_historical_duplicate(
        self,
        cur: Any,
        dup_ws: str,
        canon_ws: str,
        *,
        updated_at: str,
    ) -> int:
        await cur.execute(
            """
            UPDATE workspaces
            SET config = COALESCE(config, '{}'::jsonb)
                         || jsonb_build_object('dalton_historical_duplicate_of', %(canon)s::text),
                updated_at = %(updated_at)s::timestamptz
            WHERE workspace_id = %(dup)s::uuid
            """,
            {"dup": dup_ws, "canon": canon_ws, "updated_at": updated_at},
        )
        return int(cur.rowcount)

    # ---- Revert helpers ----

    async def verify_and_delete_merged_knowledge_versions(
        self,
        cur: Any,
        canon_ws: str,
        dup_ws: str,
        targets: list[dict[str, Any]],
    ) -> int:
        """Verify each archived version is still latest, unwritten to files, and matches merge origin, then delete."""
        if not targets:
            return 0
        sorted_targets = sorted(targets, key=lambda t: str(t["doc_key"]))
        for target in sorted_targets:
            doc_key = str(target["doc_key"])
            kv_id = str(target["knowledge_version_id"])
            expected_vno = int(target["target_version_no"])
            await self.lock_knowledge_doc(cur, canon_ws, doc_key)
            await cur.execute(
                """
                SELECT knowledge_version_id::text AS knowledge_version_id,
                       version_no, source, reason, files_written_at
                FROM dalton_knowledge_versions
                WHERE workspace_id = %s::uuid AND doc_key = %s
                ORDER BY version_no DESC
                LIMIT 1
                """,
                (canon_ws, doc_key),
            )
            latest = await cur.fetchone()
            if latest is None:
                _refuse(
                    f"cannot revert knowledge version {doc_key}: no versions found on "
                    f"canon {canon_ws} (expected {kv_id} v{expected_vno})"
                )
            if (
                str(latest["knowledge_version_id"]) != kv_id
                or int(latest["version_no"]) != expected_vno
            ):
                _refuse(
                    f"cannot revert knowledge version {doc_key}: archived "
                    f"{kv_id} (v{expected_vno}) is no longer the latest version "
                    f"(current latest is {latest['knowledge_version_id']} "
                    f"v{latest['version_no']})"
                )
            if str(latest.get("source") or "") != "server" or not str(
                latest.get("reason") or ""
            ).startswith(f"merge:{dup_ws}:"):
                _refuse(
                    f"cannot revert knowledge version {doc_key}: latest row {kv_id} "
                    f"does not match merge origin merge:{dup_ws}"
                )
            if latest.get("files_written_at") is not None:
                _refuse(
                    f"cannot revert knowledge version {doc_key}: merged row {kv_id} "
                    f"(v{expected_vno}) was already synced to workspace files at "
                    f"{latest['files_written_at']}. Deleting the DB row now would leave "
                    f"merged content in data/{doc_key}.json, rendered .md, history/, "
                    f"and agent.md for knowledge_reconcile to re-ingest. Manual fix: "
                    f"inspect ANT knowledge files against canon_existing in before.jsonl, "
                    f"restore the pre-merge files or update via knowledge_update, and "
                    f"reconcile manually."
                )

        deleted = 0
        # No try/finally around DISABLE TRIGGER: if DELETE fails inside the Postgres
        # transaction, Postgres enters InFailedSqlTransaction so a finally-block
        # ENABLE TRIGGER would fail and mask the real exception; the enclosing
        # `async with conn.transaction():` rollback automatically reverts the DDL
        # and restores trg_dalton_kv_guard.
        await cur.execute(
            f"ALTER TABLE dalton_knowledge_versions DISABLE TRIGGER {KNOWLEDGE_TRIGGER}"
        )
        for target in sorted_targets:
            doc_key = str(target["doc_key"])
            kv_id = str(target["knowledge_version_id"])
            expected_vno = int(target["target_version_no"])
            await cur.execute(
                """
                DELETE FROM dalton_knowledge_versions
                WHERE knowledge_version_id = %s::uuid
                  AND workspace_id = %s::uuid
                  AND doc_key = %s
                  AND version_no = %s
                  AND source = 'server'
                  AND reason LIKE %s
                """,
                (kv_id, canon_ws, doc_key, expected_vno, f"merge:{dup_ws}:%"),
            )
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"delete dalton_knowledge_versions rowcount={cur.rowcount} "
                    f"for {kv_id} ({doc_key} v{expected_vno})"
                )
            deleted += int(cur.rowcount)
        await cur.execute(
            f"ALTER TABLE dalton_knowledge_versions ENABLE TRIGGER {KNOWLEDGE_TRIGGER}"
        )
        return deleted

    async def restore_threads(
        self, cur: Any, records: list[dict[str, Any]]
    ) -> int:
        restored = 0
        for rec in records:
            tid = str(rec["pk"]["conversation_thread_id"])
            old = rec["old"]
            new = rec["new"]
            await cur.execute(
                """
                UPDATE conversation_threads
                SET workspace_id = %s::uuid,
                    thread_index = %s,
                    updated_at = %s
                WHERE conversation_thread_id = %s::uuid
                  AND workspace_id = %s::uuid
                  AND thread_index = %s
                """,
                (
                    old["workspace_id"],
                    int(old["thread_index"]),
                    old["updated_at"],
                    tid,
                    new["workspace_id"],
                    int(new["thread_index"]),
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"restore conversation_threads rowcount={cur.rowcount} for {tid}"
                )
            restored += int(cur.rowcount)
        return restored

    async def restore_sharing(
        self, cur: Any, canon_ws: str, existed: bool, old_row: dict[str, Any] | None
    ) -> int:
        if not existed or old_row is None:
            await cur.execute(
                "DELETE FROM dalton_workspace_sharing WHERE workspace_id = %s::uuid",
                (canon_ws,),
            )
            return int(cur.rowcount)
        await cur.execute(
            """
            UPDATE dalton_workspace_sharing
            SET cos_access = %s,
                web_access = %s,
                updated_by = %s,
                updated_at = %s
            WHERE workspace_id = %s::uuid
            """,
            (
                old_row["cos_access"],
                old_row["web_access"],
                old_row["updated_by"],
                old_row["updated_at"],
                canon_ws,
            ),
        )
        return int(cur.rowcount)

    async def restore_dup_workspace_row(
        self, cur: Any, dup_ws: str, old_values: dict[str, Any]
    ) -> int:
        old_config = old_values.get("config")
        config_param = None if old_config is None else SafeJson(old_config)
        await cur.execute(
            """
            UPDATE workspaces
            SET config = %s,
                updated_at = %s
            WHERE workspace_id = %s::uuid
            """,
            (config_param, old_values["updated_at"], dup_ws),
        )
        return int(cur.rowcount)


def _non_paused_dup_automations(plan: MergePlan) -> list[dict[str, Any]]:
    return [
        a
        for a in plan.dup_automations
        if str(a.get("workspace_id") or "") == plan.dup_ws
        and str(a.get("status") or "").strip().lower() != "paused"
    ]


def _group_outbox_by_hook_type(
    rows: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        ht = str(row.get("hook_type") or "<unknown>")
        grouped.setdefault(ht, []).append(row)
    return sorted(grouped.items(), key=lambda kv: kv[0])


def _format_outbox_row_detail(row: dict[str, Any]) -> str:
    payload = _parse_json_obj(row.get("payload"))
    refs: list[str] = []
    for key in (
        "thread_id",
        "ptc_thread_id",
        "conversation_thread_id",
        "origin_home_thread_id",
        "run_id",
        "dispatched_run_id",
        "steered_run_id",
        "task_run_id",
        "task_id",
        "command_id",
        "wakeup_id",
        "target_id",
        "execution_id",
    ):
        if payload.get(key):
            refs.append(f"{key}={payload[key]}")
    origin = payload.get("origin")
    if isinstance(origin, dict) and origin.get("origin_home_thread_id"):
        refs.append(f"origin.origin_home_thread_id={origin['origin_home_thread_id']}")
    deferred_flag = " (deferred)" if row.get("is_deferred") else ""
    ref_text = f" payload_refs=[{', '.join(refs)}]" if refs else ""
    return (
        f"    - outbox_id={row.get('hook_outbox_id')} status={row.get('status')}{deferred_flag} "
        f"thread={row.get('conversation_thread_id')} run_id={row.get('run_id')} "
        f"attempts={row.get('attempts')} created_at={row.get('created_at')} "
        f"completed_at={row.get('completed_at')} next_retry_at={row.get('next_retry_at')!r}"
        f"{ref_text}"
    )


def format_preflight_blockers(plan: MergePlan) -> str:
    """Format detailed checklist of threads/wakeups/commands/outbox/automations blocking the merge."""
    lines: list[str] = []
    shared_threads = [t for t in plan.threads if _thread_has_active_share(t)]
    if shared_threads:
        lines.append(
            f"[BLOCKER] active thread share links on dup ({len(shared_threads)}) "
            "— unshare in the app first:"
        )
        for t in shared_threads:
            lines.append(
                f"  - thread={t['conversation_thread_id']} title={t.get('title')!r} "
                f"is_shared={t.get('is_shared')} share_token={t.get('share_token')!r} "
                f"share_permissions={t.get('share_permissions')!r}"
            )
    if plan.open_wakeups:
        lines.append(
            f"[BLOCKER] unfinished wakeups / pending-delivery on dup ({len(plan.open_wakeups)}) "
            "— cancel or wait for delivery first:"
        )
        for w in plan.open_wakeups:
            lines.append(
                f"  - wakeup={w['wakeup_id']} short_id={w.get('short_id')!r} "
                f"thread={w['conversation_thread_id']} kind={w.get('kind')} "
                f"status={w.get('status')} cancelled_by={w.get('cancelled_by')!r} "
                f"label={w.get('label')!r} next_fire_at={w.get('next_fire_at')} "
                f"seq={w.get('delivered_seq')}/{w.get('last_fired_seq')}"
            )
    if plan.running_commands:
        lines.append(
            f"[BLOCKER] unfinished command_runs on dup ({len(plan.running_commands)}) "
            "— stop or wait for them first:"
        )
        for c in plan.running_commands:
            lines.append(
                f"  - command_id={c['command_id']} thread={c.get('conversation_thread_id')} "
                f"ws={c.get('workspace_id')} computer={c.get('computer_id')} "
                f"tool={c.get('tool')!r} status={c.get('status')} "
                f"started_at={c.get('started_at')}"
            )
    if plan.unreported_commands:
        lines.append(
            f"[BLOCKER] finished unreported command_runs on dup ({len(plan.unreported_commands)}) "
            "— wait for report-back delivery or close in ThreadStatus first:"
        )
        for c in plan.unreported_commands:
            lines.append(
                f"  - command_id={c['command_id']} thread={c.get('conversation_thread_id')} "
                f"ws={c.get('workspace_id')} tool={c.get('tool')!r} status={c.get('status')} "
                f"stopped_by={c.get('stopped_by')!r} ended_at={c.get('ended_at')} "
                f"outbox=({c.get('outbox_hook_type')!r}, {c.get('outbox_status')!r})"
            )
    if plan.unfinished_outbox_notifications:
        lines.append(
            f"[BLOCKER] unfinished hook_outbox notifications on dup "
            f"({len(plan.unfinished_outbox_notifications)}) — wait for delivery/expiry first:"
        )
        for hook_type, group_rows in _group_outbox_by_hook_type(
            plan.unfinished_outbox_notifications
        ):
            lines.append(f"  hook_type={hook_type} ({len(group_rows)}):")
            for row in group_rows:
                lines.append(_format_outbox_row_detail(row))
    non_paused_autos = _non_paused_dup_automations(plan)
    if non_paused_autos:
        lines.append(
            f"[BLOCKER] non-paused automations on dup ({len(non_paused_autos)}) "
            "— pause them first:"
        )
        for a in non_paused_autos:
            lines.append(
                f"  - automation_id={a['automation_id']} name={a.get('name')!r} "
                f"status={a.get('status')!r} thread_strategy={a.get('thread_strategy')!r} "
                f"pinned_thread={a.get('conversation_thread_id')!r}"
            )
    if plan.unsettled_automation_executions:
        lines.append(
            f"[BLOCKER] pending/waiting/running automation_executions on dup "
            f"({len(plan.unsettled_automation_executions)}) — wait or cancel first:"
        )
        for ex in plan.unsettled_automation_executions:
            lines.append(
                f"  - execution_id={ex['automation_execution_id']} "
                f"automation_id={ex.get('automation_id')} name={ex.get('automation_name')!r} "
                f"status={ex.get('status')!r} thread={ex.get('conversation_thread_id')} "
                f"scheduled_at={ex.get('scheduled_at')} started_at={ex.get('started_at')}"
            )
    if plan.invalid_knowledge_versions:
        lines.append(
            f"[BLOCKER] invalid latest dalton_knowledge_versions on dup "
            f"({len(plan.invalid_knowledge_versions)}) — schema_valid=false:"
        )
        for kv in plan.invalid_knowledge_versions:
            lines.append(
                f"  - doc_key={kv['doc_key']} version_no=v{kv.get('version_no')} "
                f"kv_id={kv.get('knowledge_version_id')} "
                f"schema_valid={kv.get('schema_valid')} source={kv.get('source')!r} "
                f"edited_path={kv.get('edited_path')!r}"
            )
        lines.append(
            "  处理方法：先在副本工作区修好这份文档，使它的最新版本通过校验（schema_valid=true），"
            "再重试合并。脚本没有跳过某份文档的选项。"
        )
    return "\n".join(lines)


async def build_merge_plan(
    db: MergeDb,
    cur: Any,
    *,
    dup_ws: str,
    canon_ws: str,
    tx_now: str | None = None,
) -> MergePlan:
    """Run all preflight validations and compute the deterministic merge plan."""
    dup_id = normalize_uuid(dup_ws)
    canon_id = normalize_uuid(canon_ws)
    if not dup_id:
        _refuse(f"invalid --dup workspace UUID: {dup_ws!r}")
    if not canon_id:
        _refuse(f"invalid --canon workspace UUID: {canon_ws!r}")
    if dup_id == canon_id:
        _refuse(f"--dup and --canon must be different workspaces ({dup_id})")

    dup_row = await db.fetch_workspace(cur, dup_id)
    if dup_row is None:
        _refuse(f"duplicate workspace {dup_id} does not exist")
    canon_row = await db.fetch_workspace(cur, canon_id)
    if canon_row is None:
        _refuse(f"canonical workspace {canon_id} does not exist")

    for label, row in (("dup", dup_row), ("canon", canon_row)):
        ws_id = str(row["workspace_id"])
        status = str(row.get("status") or "")
        if status == "deleted":
            _refuse(f"{label} workspace {ws_id} is deleted")
        if status == "unbound" or _is_home_workspace(row):
            _refuse(f"{label} workspace {ws_id} is a Home/unbound workspace")

    if is_historical_duplicate_workspace(dup_row):
        existing_marker = _parse_config(dup_row.get("config")).get(
            "dalton_historical_duplicate_of"
        )
        _refuse(
            f"dup workspace {dup_id} already carries "
            f"dalton_historical_duplicate_of={existing_marker!r}"
        )

    canon_unit = await db.fetch_active_coverage_unit(cur, canon_id)
    if canon_unit is None:
        _refuse(f"canon workspace {canon_id} is not bound to an active coverage unit")
    if str(canon_unit.get("kind") or "").strip().lower() == "sector":
        _refuse(
            f"canon workspace {canon_id} is a sector workspace "
            f"(unit={canon_unit.get('unit_id')}, slug={canon_unit.get('slug')!r}); "
            "only company workspaces are allowed"
        )
    if str(canon_unit.get("kind") or "").strip().lower() != "company":
        _refuse(
            f"canon workspace {canon_id} coverage unit has unsupported kind "
            f"{canon_unit.get('kind')!r}; only company workspaces are allowed"
        )

    dup_unit = await db.fetch_active_coverage_unit(cur, dup_id)
    if dup_unit is not None:
        if str(dup_unit.get("kind") or "").strip().lower() == "sector":
            _refuse(
                f"dup workspace {dup_id} is a sector workspace "
                f"(unit={dup_unit.get('unit_id')}, slug={dup_unit.get('slug')!r}); "
                "only company workspaces are allowed"
            )
        _refuse(
            f"dup workspace {dup_id} is bound to active coverage unit "
            f"{dup_unit.get('unit_id')} (slug={dup_unit.get('slug')!r})"
        )

    dup_latest_kv = await db.fetch_latest_knowledge_versions(cur, dup_id)
    canon_latest_kv = await db.fetch_latest_knowledge_versions(cur, canon_id)
    for label, ws_id, latest_kv in (
        ("dup", dup_id, dup_latest_kv),
        ("canon", canon_id, canon_latest_kv),
    ):
        if "sector" in latest_kv:
            _refuse(
                f"{label} workspace {ws_id} is a sector workspace "
                "(has 'sector' knowledge document); only company workspaces are allowed"
            )

    threads = await db.fetch_threads_for_workspace(cur, dup_id)
    dup_thread_ids = [str(t["conversation_thread_id"]) for t in threads]

    dup_automations = await db.fetch_automations_for_workspace(
        cur, dup_id, thread_ids=dup_thread_ids
    )
    canon_automations = await db.fetch_automations_for_workspace(cur, canon_id)
    for label, ws_id, autos in (
        ("dup", dup_id, dup_automations),
        ("canon", canon_id, canon_automations),
    ):
        for auto in autos:
            if str(auto.get("workspace_id") or "") == ws_id and _is_sector_automation(auto):
                _refuse(
                    f"{label} workspace {ws_id} is a sector workspace "
                    f"(has sector automation {auto.get('name')!r}); "
                    "only company workspaces are allowed"
                )

    open_wakeups = await db.fetch_open_wakeups_for_workspace(cur, dup_id)
    running_commands = await db.fetch_running_command_runs_for_workspace(cur, dup_id)
    unreported_commands = await db.fetch_unreported_command_runs_for_workspace(
        cur, dup_id
    )
    unfinished_outbox = await db.fetch_unfinished_outbox_notifications_for_workspace(
        cur, dup_id
    )
    queued_file_comments = await db.fetch_queued_file_comments_for_workspace(
        cur, dup_id
    )
    dup_auto_ids = [str(a["automation_id"]) for a in dup_automations if a.get("automation_id")]
    unsettled_executions = await db.fetch_unsettled_automation_executions(
        cur, dup_auto_ids, dup_thread_ids
    )
    retained_research_counts = await db.fetch_dup_retained_research_counts(
        cur, dup_id, str(dup_row.get("user_id") or "")
    )
    watermark = await db.fetch_db_watermark(cur, canon_id, dup_id)

    for label, ws_id in (("dup", dup_id), ("canon", canon_id)):
        activity = await db.fetch_live_activity(cur, ws_id)
        if not activity["busy"]:
            continue
        if (
            label == "dup"
            and not activity.get("has_live_turns_or_subagents")
            and int(activity.get("live_responses", 0)) == 0
            and int(activity.get("live_subagents", 0)) == 0
            and running_commands
        ):
            # Only command_runs are running on dup; fall through to the unified
            # blocker report below so full command_id details are printed.
            continue
        cmd_list = (
            running_commands
            if label == "dup"
            else (
                await db.fetch_running_command_runs_for_workspace(cur, canon_id)
                if int(activity.get("live_commands", 0)) > 0
                else []
            )
        )
        cmd_ids = [str(c["command_id"]) for c in cmd_list]
        if cmd_ids:
            print(
                f"[BLOCKER] running command_runs on {label} ({len(cmd_ids)}): "
                + ", ".join(cmd_ids)
            )
        cmd_suffix = f" running_command_ids={cmd_ids}" if cmd_ids else ""
        _refuse(
            f"{label} workspace {ws_id} has live work in progress: "
            f"responses={activity['live_responses']}, "
            f"subagent_runs={activity['live_subagents']}, "
            f"command_runs={activity['live_commands']}{cmd_suffix}"
        )

    canon_next_idx = await db.fetch_next_thread_index(cur, canon_id)
    thread_moves = [
        ThreadMoveItem(
            row=t,
            target_workspace_id=canon_id,
            target_thread_index=canon_next_idx + idx,
        )
        for idx, t in enumerate(threads)
    ]

    canon_all_kv = await db.fetch_all_knowledge_versions(cur, canon_id)
    canon_unit_ticker = str(canon_unit.get("ticker") or "").strip().upper() or None
    knowledge_items: list[KnowledgeMergeItem] = []
    invalid_knowledge: list[dict[str, Any]] = []
    for doc_key in sorted(dup_latest_kv):
        dup_kv = dup_latest_kv[doc_key]
        if not bool(dup_kv.get("schema_valid", True)):
            invalid_knowledge.append(
                {
                    "doc_key": doc_key,
                    "version_no": dup_kv.get("version_no"),
                    "knowledge_version_id": dup_kv.get("knowledge_version_id"),
                    "schema_valid": dup_kv.get("schema_valid"),
                    "source": dup_kv.get("source"),
                    "edited_path": dup_kv.get("edited_path"),
                }
            )
        canon_kv = canon_latest_kv.get(doc_key)
        target_vno = (int(canon_kv["version_no"]) + 1) if canon_kv else 1
        raw_content = dup_kv.get("content")
        merged_content = copy.deepcopy(raw_content)
        original_content_ticker: str | None = None
        ticker_rewrite_note: str | None = None
        raw_ticker_val = (
            raw_content.get("ticker")
            if isinstance(raw_content, dict)
            else None
        )
        raw_content_ticker = (
            str(raw_ticker_val)
            if isinstance(raw_ticker_val, str) and raw_ticker_val.strip()
            else None
        )
        if isinstance(merged_content, dict) and raw_content_ticker is not None:
            rewritten_ticker, rewrite_note = _resolve_content_ticker_rewrite(
                canon_unit, raw_content_ticker
            )
            if rewritten_ticker is not None:
                original_content_ticker = str(merged_content.get("ticker"))
                merged_content["ticker"] = rewritten_ticker
            elif rewrite_note is not None:
                ticker_rewrite_note = rewrite_note
                print(f"[INFO] doc_key={doc_key}: {rewrite_note}")
        ticker = (
            canon_unit_ticker
            or (str(canon_kv.get("ticker") or "").strip().upper() if canon_kv else None)
            or (raw_content_ticker.strip().upper() if raw_content_ticker else None)
            or (str(dup_kv.get("ticker") or "").strip().upper() or None)
        )
        knowledge_items.append(
            KnowledgeMergeItem(
                doc_key=doc_key,
                dup_latest=dup_kv,
                canon_latest=canon_kv,
                target_knowledge_version_id=str(uuid.uuid4()),
                target_version_no=target_vno,
                reason=_build_merge_reason(
                    dup_id,
                    str(dup_row.get("name") or ""),
                    doc_key,
                    int(dup_kv["version_no"]),
                ),
                content_sha256=_compute_content_sha256(
                    merged_content, str(dup_kv.get("content_sha256") or "")
                ),
                ticker=ticker,
                content=merged_content,
                original_content_ticker=original_content_ticker,
                ticker_rewrite_note=ticker_rewrite_note,
            )
        )

    dup_sharing = await db.fetch_sharing_row(cur, dup_id)
    canon_sharing = await db.fetch_sharing_row(cur, canon_id)
    dup_new_config = _parse_config(dup_row.get("config"))
    dup_new_config["dalton_historical_duplicate_of"] = canon_id
    db_now = watermark.get("db_now")
    effective_now = tx_now or (str(db_now) if db_now else "")
    if not effective_now:
        raise RuntimeError("database NOW() watermark is required to build merge plan")
    canon_max_run_seq = int(watermark.get("canon_max_run_seq") or 0)
    dup_max_run_seq = int(watermark.get("dup_max_run_seq") or 0)

    plan = MergePlan(
        dup_ws=dup_id,
        canon_ws=canon_id,
        tx_now=effective_now,
        dup_workspace=dup_row,
        canon_workspace=canon_row,
        canon_unit=canon_unit,
        canon_max_run_seq=canon_max_run_seq,
        dup_max_run_seq=dup_max_run_seq,
        thread_moves=thread_moves,
        canon_next_thread_index=canon_next_idx,
        open_wakeups=open_wakeups,
        running_commands=running_commands,
        unreported_commands=unreported_commands,
        unfinished_outbox_notifications=unfinished_outbox,
        queued_file_comments=queued_file_comments,
        dup_automations=dup_automations,
        unsettled_automation_executions=unsettled_executions,
        retained_research_counts=retained_research_counts,
        knowledge_items=knowledge_items,
        invalid_knowledge_versions=invalid_knowledge,
        canon_existing_knowledge=canon_all_kv,
        dup_sharing=dup_sharing,
        canon_sharing=canon_sharing,
        dup_new_config=dup_new_config,
    )

    shared_threads = [t for t in threads if _thread_has_active_share(t)]
    non_paused_autos = _non_paused_dup_automations(plan)
    if (
        shared_threads
        or open_wakeups
        or running_commands
        or unreported_commands
        or unfinished_outbox
        or non_paused_autos
        or unsettled_executions
        or invalid_knowledge
    ):
        blocker_report = format_preflight_blockers(plan)
        print(format_plan_report(plan))
        print("")
        print(blocker_report)
        reasons: list[str] = []
        if shared_threads:
            reasons.append(f"{len(shared_threads)} active thread share link(s)")
        if open_wakeups:
            reasons.append(
                f"{len(open_wakeups)} unfinished wakeup(s) (open or pending-delivery)"
            )
        if running_commands:
            reasons.append(f"{len(running_commands)} running command_runs")
        if unreported_commands:
            reasons.append(f"{len(unreported_commands)} unreported finished command_runs")
        if unfinished_outbox:
            reasons.append(
                f"{len(unfinished_outbox)} unfinished hook_outbox notification(s)"
            )
        if non_paused_autos:
            reasons.append(f"{len(non_paused_autos)} non-paused automation(s)")
        if unsettled_executions:
            reasons.append(
                f"{len(unsettled_executions)} pending/waiting/running automation execution(s)"
            )
        if invalid_knowledge:
            invalid_desc = ", ".join(
                f"{kv['doc_key']} v{kv.get('version_no')}"
                for kv in invalid_knowledge
            )
            reasons.append(
                f"{len(invalid_knowledge)} schema-invalid latest knowledge version(s) "
                f"({invalid_desc})"
            )
        _refuse(
            "cannot merge while dup has "
            + ", ".join(reasons)
            + "; resolve manually first:\n"
            + blocker_report
        )

    return plan


def format_plan_report(plan: MergePlan) -> str:
    """Format the human-readable preflight summary for dry-run and apply."""
    lines: list[str] = []
    counts = plan.counts()
    dup_thread_ids = {str(t["conversation_thread_id"]) for t in plan.threads}

    lines.append("=== Workspace Merge Preflight ===")
    lines.append(
        f"dup  : {plan.dup_ws} name={plan.dup_workspace.get('name')!r} "
        f"owner={plan.dup_workspace.get('user_id')} "
        f"computer={plan.dup_workspace.get('computer_id')}"
    )
    lines.append(
        f"canon: {plan.canon_ws} name={plan.canon_workspace.get('name')!r} "
        f"owner={plan.canon_workspace.get('user_id')} "
        f"computer={plan.canon_workspace.get('computer_id')} "
        f"unit={plan.canon_unit.get('unit_id')} ticker={plan.canon_unit.get('ticker')!r}"
    )
    lines.append("")
    lines.append(f"[conversation_threads] to move: {counts['conversation_threads']}")
    for m in plan.thread_moves:
        t = m.row
        lines.append(
            f"  - id={t['conversation_thread_id']} title={t.get('title')!r} "
            f"user_id={t.get('user_id')!r} created_by={t.get('created_by')!r} "
            f"status={t.get('current_status')} "
            f"thread_index={t.get('thread_index')}->{m.target_thread_index} "
            f"is_shared={t.get('is_shared')} share_token={t.get('share_token')!r} "
            f"share_permissions={t.get('share_permissions')!r}"
        )

    lines.append(f"[wakeups(unfinished/pending-delivery)] on dup: {len(plan.open_wakeups)}")
    for w in plan.open_wakeups:
        lines.append(
            f"  - wakeup_id={w['wakeup_id']} short_id={w.get('short_id')!r} "
            f"thread={w['conversation_thread_id']} kind={w.get('kind')} "
            f"status={w.get('status')} cancelled_by={w.get('cancelled_by')!r} "
            f"next_fire_at={w.get('next_fire_at')}"
        )

    lines.append(f"[command_runs(running)] on dup: {len(plan.running_commands)}")
    for c in plan.running_commands:
        lines.append(
            f"  - command_id={c['command_id']} thread={c.get('conversation_thread_id')} "
            f"tool={c.get('tool')!r} status={c.get('status')} started_at={c.get('started_at')}"
        )

    lines.append(f"[command_runs(finished-unreported)] on dup: {len(plan.unreported_commands)}")
    for c in plan.unreported_commands:
        lines.append(
            f"  - command_id={c['command_id']} thread={c.get('conversation_thread_id')} "
            f"tool={c.get('tool')!r} status={c.get('status')} "
            f"stopped_by={c.get('stopped_by')!r} ended_at={c.get('ended_at')} "
            f"outbox=({c.get('outbox_hook_type')!r}, {c.get('outbox_status')!r})"
        )

    lines.append(
        f"[hook_outbox(unfinished)] on dup: {len(plan.unfinished_outbox_notifications)}"
    )
    for hook_type, group_rows in _group_outbox_by_hook_type(
        plan.unfinished_outbox_notifications
    ):
        lines.append(f"  hook_type={hook_type} ({len(group_rows)}):")
        for row in group_rows:
            lines.append(_format_outbox_row_detail(row))

    lines.append(
        f"[artifact_comments(queued)] on dup threads referencing dup files "
        f"(提示 only, does NOT block): {len(plan.queued_file_comments)}"
    )
    for c in plan.queued_file_comments:
        lines.append(
            f"  - comment_id={c['comment_id']} number=#{c.get('number')} "
            f"thread={c.get('thread_id')} ({c.get('thread_title')!r}) "
            f"artifact={c.get('artifact_id')} path={c.get('path')!r} "
            f"title={c.get('title')!r} user_id={c.get('user_id')!r} "
            "(迁移后将无法在 ANT 对话内直接发送此副本文件评论，建议合并前发送或处理)"
        )

    lines.append(f"[automations] on dup / referencing dup threads (unchanged): {len(plan.dup_automations)}")
    for a in plan.dup_automations:
        pinned_tid = a.get("conversation_thread_id")
        strategy = str(a.get("thread_strategy") or "")
        lines.append(
            f"  - automation_id={a['automation_id']} name={a.get('name')!r} "
            f"status={a.get('status')!r} thread_strategy={strategy!r} "
            f"pinned_thread={pinned_tid!r}"
        )
        if pinned_tid and str(pinned_tid) in dup_thread_ids:
            lines.append(
                f"    NOTICE: automation {a['automation_id']} ({a.get('name')!r}, "
                f"status={a.get('status')!r}, thread_strategy={strategy!r}) is pinned "
                f"to migrating thread {pinned_tid}; automation will NOT be modified."
            )

    lines.append(
        f"[automation_executions(pending/waiting/running)] on dup: "
        f"{len(plan.unsettled_automation_executions)}"
    )
    for ex in plan.unsettled_automation_executions:
        lines.append(
            f"  - execution_id={ex['automation_execution_id']} "
            f"automation_id={ex.get('automation_id')} name={ex.get('automation_name')!r} "
            f"status={ex.get('status')!r} thread={ex.get('conversation_thread_id')} "
            f"scheduled_at={ex.get('scheduled_at')}"
        )

    lines.append(
        f"[dalton_knowledge_versions] to insert on canon: "
        f"{counts['dalton_knowledge_versions']}"
    )
    for item in plan.knowledge_items:
        canon_v = item.canon_latest["version_no"] if item.canon_latest else None
        rewritten_ticker = (
            item.content.get("ticker")
            if isinstance(item.content, dict)
            else item.ticker
        )
        ticker_note = (
            f" content_ticker={item.original_content_ticker}->{rewritten_ticker}"
            if item.original_content_ticker
            else ""
        )
        lines.append(
            f"  - doc_key={item.doc_key} kv_id={item.target_knowledge_version_id} "
            f"dup_v={item.dup_latest['version_no']} "
            f"(schema_valid={item.dup_latest.get('schema_valid')}) -> "
            f"canon_existing_v={canon_v} -> new_v={item.target_version_no}{ticker_note} "
            f"source='server' reason={item.reason!r}"
        )
        if item.ticker_rewrite_note:
            lines.append(f"    NOTE: {item.ticker_rewrite_note}")
    lines.append(
        f"[dalton_workspace_sharing] canon current={plan.canon_sharing}, "
        f"dup current={plan.dup_sharing} -> target cos_access='rw', web_access='rw', "
        f"updated_by={plan.canon_workspace.get('user_id')!r}"
    )
    lines.append(
        f"[workspaces] dup config += dalton_historical_duplicate_of={plan.canon_ws!r} "
        "(canon workspaces row unchanged; workspace_files/command_runs/chart_annotations/share_links unchanged)"
    )
    lines.append("[retained_research_records] 留在副本 (not moved to canon):")
    for table_name in (
        "dalton_verdicts",
        "dalton_signals",
        "dalton_publications",
        "dalton_publish_requests",
        "workspace_memory",
    ):
        lines.append(
            f"  - {table_name}: {int(plan.retained_research_counts.get(table_name, 0))} (留在副本)"
        )
    return "\n".join(lines)


def build_archive_records(
    plan: MergePlan, *, timestamp: str | None = None
) -> list[dict[str, Any]]:
    """Build JSONL records capturing PKs, pre-mutation old values, and planned new values."""
    canon_owner = plan.canon_workspace.get("user_id")
    meta_ts = timestamp or plan.tx_now
    records: list[dict[str, Any]] = [
        {
            "record_type": "meta",
            "version": 2,
            "created_at": meta_ts,
            "tx_now": plan.tx_now,
            "canon_max_run_seq": int(plan.canon_max_run_seq),
            "dup_max_run_seq": int(plan.dup_max_run_seq),
            "dup_ws": plan.dup_ws,
            "canon_ws": plan.canon_ws,
            "dup_user_id": plan.dup_workspace.get("user_id"),
            "canon_user_id": canon_owner,
            "counts": plan.counts(),
        },
        {
            "record_type": "workspaces",
            "role": "dup",
            "action": "update",
            "pk": {"workspace_id": plan.dup_ws},
            "old": {
                "config": plan.dup_workspace.get("config"),
                "updated_at": plan.dup_workspace.get("updated_at"),
            },
            "new": {
                "config": plan.dup_new_config,
                "updated_at": plan.tx_now,
            },
            "row": plan.dup_workspace,
        },
        {
            "record_type": "workspaces",
            "role": "canon",
            "action": "unchanged",
            "pk": {"workspace_id": plan.canon_ws},
            "old": {
                "config": plan.canon_workspace.get("config"),
                "updated_at": plan.canon_workspace.get("updated_at"),
            },
            "new": {
                "config": plan.canon_workspace.get("config"),
                "updated_at": plan.canon_workspace.get("updated_at"),
            },
            "row": plan.canon_workspace,
        },
        {
            "record_type": "dalton_workspace_sharing",
            "role": "canon",
            "action": "update" if plan.canon_sharing is not None else "insert",
            "existed": plan.canon_sharing is not None,
            "pk": {"workspace_id": plan.canon_ws},
            "old": copy.deepcopy(plan.canon_sharing),
            "new": {
                "workspace_id": plan.canon_ws,
                "cos_access": "rw",
                "web_access": "rw",
                "updated_by": canon_owner,
                "updated_at": plan.tx_now,
            },
            "row": plan.canon_sharing,
        },
        {
            "record_type": "dalton_workspace_sharing",
            "role": "dup",
            "action": "unchanged",
            "existed": plan.dup_sharing is not None,
            "pk": {"workspace_id": plan.dup_ws},
            "row": plan.dup_sharing,
        },
    ]
    for m in plan.thread_moves:
        row = m.row
        records.append(
            {
                "record_type": "conversation_threads",
                "action": "update",
                "pk": {"conversation_thread_id": row["conversation_thread_id"]},
                "old": {
                    "workspace_id": row["workspace_id"],
                    "thread_index": int(row["thread_index"]),
                    "updated_at": row.get("updated_at"),
                },
                "new": {
                    "workspace_id": m.target_workspace_id,
                    "thread_index": int(m.target_thread_index),
                    "updated_at": row.get("updated_at"),
                },
                "row": row,
            }
        )
    for kv in plan.canon_existing_knowledge:
        records.append(
            {
                "record_type": "dalton_knowledge_versions",
                "role": "canon_existing",
                "action": "unchanged",
                "pk": {"knowledge_version_id": kv["knowledge_version_id"]},
                "row": kv,
            }
        )
    for item in plan.knowledge_items:
        records.append(
            {
                "record_type": "dalton_knowledge_versions",
                "role": "dup_source",
                "action": "unchanged",
                "pk": {"knowledge_version_id": item.dup_latest["knowledge_version_id"]},
                "row": item.dup_latest,
            }
        )
        records.append(
            {
                "record_type": "knowledge_plan",
                "action": "insert",
                "pk": {"knowledge_version_id": item.target_knowledge_version_id},
                "knowledge_version_id": item.target_knowledge_version_id,
                "workspace_id": plan.canon_ws,
                "doc_key": item.doc_key,
                "target_version_no": item.target_version_no,
                "source_knowledge_version_id": item.dup_latest["knowledge_version_id"],
                "reason": item.reason,
                "old": (
                    {
                        "knowledge_version_id": item.canon_latest["knowledge_version_id"],
                        "version_no": int(item.canon_latest["version_no"]),
                    }
                    if item.canon_latest is not None
                    else None
                ),
                "new": {
                    "knowledge_version_id": item.target_knowledge_version_id,
                    "workspace_id": plan.canon_ws,
                    "doc_key": item.doc_key,
                    "version_no": item.target_version_no,
                    "source": "server",
                    "user_id": canon_owner,
                    "ticker": item.ticker,
                    "content_sha256": item.content_sha256,
                    "reason": item.reason,
                },
            }
        )
    return records


def _fsync_dir(dir_path: Path) -> None:
    """Flush directory entry metadata to disk."""
    dir_fd = os.open(dir_path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_archive_jsonl(records: list[dict[str, Any]], archive_path: Path) -> str:
    """Create ``before.jsonl`` exclusively with 0600 permissions and ``fsync`` file + directories."""
    lines = [
        json.dumps(rec, ensure_ascii=True, default=_json_default)
        for rec in records
    ]
    payload = "\n".join(lines) + "\n"

    # Track directories that mkdir(parents=True) will create so we fsync
    # archive_path.parent AND the parent directory of every newly created dir.
    missing_ancestors: list[Path] = []
    probe = archive_path.parent
    while not probe.exists():
        missing_ancestors.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(archive_path, flags, 0o600)
    except FileExistsError as exc:
        raise MergeRefusedError(
            f"REFUSED: archive path already exists (refusing to overwrite): {archive_path}"
        ) from exc

    wrote_ok = False
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1  # owned by fh now
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        dirs_to_sync: list[Path] = [archive_path.parent]
        if missing_ancestors:
            for dir_path in [*missing_ancestors[1:], probe]:
                if dir_path not in dirs_to_sync:
                    dirs_to_sync.append(dir_path)
        for dir_path in dirs_to_sync:
            _fsync_dir(dir_path)
        wrote_ok = True
    finally:
        if fd >= 0:
            os.close(fd)
        if not wrote_ok:
            try:
                archive_path.unlink(missing_ok=True)
            except OSError:
                pass
    return str(archive_path)


def load_archive_jsonl(archive_path: Path) -> list[dict[str, Any]]:
    if not archive_path.is_file():
        _refuse(f"archive file not found: {archive_path}")
    records: list[dict[str, Any]] = []
    # Split strictly on '\n' (never splitlines()) so Unicode line separators
    # like U+2028/U+2029 inside raw text cannot split a JSON record.
    for line_no, raw_line in enumerate(
        archive_path.read_text(encoding="utf-8").split("\n"), start=1
    ):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            _refuse(f"invalid JSON in {archive_path}:{line_no}: {exc}")
        if not isinstance(obj, dict) or "record_type" not in obj:
            _refuse(f"malformed archive record in {archive_path}:{line_no}")
        records.append(obj)
    return records


def _assert_rowcount(step: str, actual: int, expected: int) -> None:
    if actual != expected:
        raise RuntimeError(
            f"rowcount mismatch on {step}: expected {expected}, got {actual} (rolled back)"
        )


def _format_follow_up_notes() -> str:
    return "\n".join(["后续说明：", *FOLLOW_UP_NOTES])


def _format_post_commit_share_alarm(shared_threads: list[dict[str, Any]]) -> str:
    lines = [
        "=" * 72,
        "CRITICAL ALARM: CONCURRENT THREAD SHARING DETECTED AFTER MERGE COMMIT",
        f"({len(shared_threads)} migrated thread(s) currently have is_shared=TRUE in canon/ANT!)",
        "A user or colleague enabled sharing while the merge was in flight; the share",
        "link now resolves inside canon (ANT) and could expose ANT workspace files.",
        "Affected threads:",
    ]
    for t in shared_threads:
        lines.append(
            f"  - thread={t['conversation_thread_id']} title={t.get('title')!r} "
            f"share_token={t.get('share_token')!r} "
            f"share_permissions={t.get('share_permissions')!r} "
            f"shared_at={t.get('shared_at')}"
        )
    lines.extend(
        [
            "立即取消分享的方法（任选其一）：",
            "  1. 在 Web 应用中打开 ANT 工作区对应对话 -> 点击 Share -> 关闭公开分享开关。",
            "  2. 或在数据库执行紧急关闭（保留 token 但停用访问）：",
            "     UPDATE conversation_threads SET is_shared = FALSE, updated_at = NOW()",
            "     WHERE conversation_thread_id IN ("
            + ", ".join(f"'{t['conversation_thread_id']}'::uuid" for t in shared_threads)
            + ");",
            "=" * 72,
        ]
    )
    return "\n".join(lines)


def _format_post_commit_recheck_failed_alarm(
    *,
    canon_ws: str,
    moved_thread_ids: list[str],
    error: BaseException,
) -> str:
    id_list = ", ".join(f"'{tid}'::uuid" for tid in moved_thread_ids)
    sql = (
        "SELECT conversation_thread_id, title, is_shared, share_token, "
        "share_permissions, shared_at\n"
        "     FROM conversation_threads\n"
        f"     WHERE workspace_id = '{canon_ws}'::uuid\n"
        f"       AND conversation_thread_id IN ({id_list})\n"
        "       AND (is_shared IS TRUE OR share_token IS NOT NULL);"
    )
    return "\n".join(
        [
            "=" * 72,
            "WARNING: 分享状态未能复查 (POST-COMMIT SHARE RECHECK FAILED)",
            "数据库合并事务已提交成功；仅提交后的只读分享复查失败：",
            f"  error={error}",
            "请立即手动执行以下 SQL 核查迁入对话是否处于公开分享状态：",
            f"     {sql}",
            "=" * 72,
        ]
    )


async def run_merge(
    *,
    dup_ws: str,
    canon_ws: str,
    apply: bool = False,
    archive_out: str | None = None,
    dsn: str | None = None,
    db: MergeDb | None = None,
) -> dict[str, Any]:
    """Execute dry-run preflight or transactional ``--apply`` merge."""
    store = db or MergeDb()
    dup_norm = normalize_uuid(dup_ws)
    canon_norm = normalize_uuid(canon_ws)
    if not dup_norm:
        _refuse(f"invalid --dup workspace UUID: {dup_ws!r}")
    if not canon_norm:
        _refuse(f"invalid --canon workspace UUID: {canon_ws!r}")
    if archive_out == "-":
        _refuse("--archive-out '-' (stdout) is not supported; provide a file path")

    if not apply:
        async with store.connect(dsn=dsn, read_only=True) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                plan = await build_merge_plan(
                    store,
                    cur,
                    dup_ws=dup_norm,
                    canon_ws=canon_norm,
                )
            await conn.rollback()
        print(format_plan_report(plan))
        print("\nDRY RUN: nothing modified (re-run with --apply to execute).")
        print(_format_follow_up_notes())
        return {"applied": False, "counts": plan.counts()}

    actual_counts: dict[str, int] = {}
    archive_location = ""
    moved_thread_ids: list[str] = []

    try:
        async with store.connect(dsn=dsn, read_only=False) as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    # 1. Lock timeout + application-consistent lock order:
                    #    thread_index advisory (home_fold.py:151) ->
                    #    dalton_workspace_sharing FOR UPDATE before workspaces FOR UPDATE
                    #    (src/dalton/sharing.py:261-278) ->
                    #    workspaces FOR UPDATE -> conversation_threads FOR UPDATE ->
                    #    knowledge doc advisory locks (knowledge.py:163).
                    await store.set_lock_timeout(cur, LOCK_TIMEOUT)
                    for ws_id in sorted([dup_norm, canon_norm]):
                        await store.lock_thread_index(cur, ws_id)
                    await store.lock_sharing_for_update(cur, canon_norm)
                    await store.lock_workspaces_for_update(cur, [dup_norm, canon_norm])
                    await store.lock_threads_for_update(cur, dup_norm)

                    dup_doc_keys = await store.fetch_knowledge_doc_keys(cur, dup_norm)
                    canon_doc_keys = await store.fetch_knowledge_doc_keys(cur, canon_norm)
                    all_doc_keys = sorted(set(dup_doc_keys) | set(canon_doc_keys))
                    for ws_id in sorted([dup_norm, canon_norm]):
                        for doc_key in all_doc_keys:
                            await store.lock_knowledge_doc(cur, ws_id, doc_key)

                    # 2. Build plan from locked rows and DB watermark (no Python clock).
                    locked_plan = await build_merge_plan(
                        store,
                        cur,
                        dup_ws=dup_norm,
                        canon_ws=canon_norm,
                    )
                    expected = locked_plan.counts()
                    moved_thread_ids = [
                        str(m.row["conversation_thread_id"])
                        for m in locked_plan.thread_moves
                    ]
                    parsed_tx_now = _parse_ts(locked_plan.tx_now)
                    stamp = (
                        parsed_tx_now.strftime("%Y%m%dT%H%M%SZ")
                        if parsed_tx_now is not None
                        else "".join(
                            ch if ch.isalnum() else "-" for ch in str(locked_plan.tx_now)
                        )
                    )
                    archive_path = (
                        Path(archive_out).expanduser()
                        if archive_out
                        else _default_archive_path(stamp)
                    )

                    # 3. Write and fsync archive (file + directories) BEFORE any DB write.
                    #    Any failure rolls back the transaction with zero writes.
                    print(format_plan_report(locked_plan))
                    archive_location = write_archive_jsonl(
                        build_archive_records(locked_plan, timestamp=locked_plan.tx_now),
                        archive_path,
                    )
                    print(f"\nArchive written: {archive_location}")

                    # 4. Execute mutations strictly from locked_plan.
                    moved_threads = await store.move_threads(
                        cur, dup_norm, canon_norm, locked_plan.thread_moves
                    )
                    _assert_rowcount(
                        "conversation_threads",
                        moved_threads,
                        expected["conversation_threads"],
                    )
                    actual_counts["conversation_threads"] = moved_threads

                    canon_owner = str(locked_plan.canon_workspace["user_id"])
                    inserted_kv = 0
                    for item in locked_plan.knowledge_items:
                        await store.insert_merged_knowledge_version(
                            cur,
                            canon_user_id=canon_owner,
                            canon_ws=canon_norm,
                            dup_ws=dup_norm,
                            item=item,
                        )
                        inserted_kv += 1
                    _assert_rowcount(
                        "dalton_knowledge_versions",
                        inserted_kv,
                        expected["dalton_knowledge_versions"],
                    )
                    actual_counts["dalton_knowledge_versions"] = inserted_kv

                    sharing_rows = await store.upsert_canon_sharing(
                        cur, canon_norm, canon_owner, updated_at=locked_plan.tx_now
                    )
                    _assert_rowcount(
                        "dalton_workspace_sharing",
                        sharing_rows,
                        expected["dalton_workspace_sharing"],
                    )
                    actual_counts["dalton_workspace_sharing"] = sharing_rows

                    dup_marked = await store.mark_dup_historical_duplicate(
                        cur, dup_norm, canon_norm, updated_at=locked_plan.tx_now
                    )
                    _assert_rowcount(
                        "workspaces(dup)",
                        dup_marked,
                        expected["workspaces_dup_marked"],
                    )
                    actual_counts["workspaces_dup_marked"] = dup_marked
    except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected) as exc:
        cleanup_note = ""
        if archive_location:
            try:
                Path(archive_location).unlink(missing_ok=True)
                cleanup_note = f" Aborted archive file deleted: {archive_location}."
            except OSError as rm_exc:
                cleanup_note = (
                    f" Warning: failed to delete aborted archive file "
                    f"{archive_location}: {rm_exc}."
                )
        if isinstance(exc, psycopg.errors.DeadlockDetected):
            raise MergeRefusedError(
                "REFUSED: deadlock detected during --apply "
                "(another session locked or updated a row in a conflicting order; "
                f"database transaction rolled back with no DB writes).{cleanup_note} "
                "Retry when idle."
            ) from exc
        raise MergeRefusedError(
            f"REFUSED: lock_timeout ({LOCK_TIMEOUT}) exceeded during --apply "
            "(another session holds a lock on dup/canon workspaces, sharing, or "
            f"threads; database transaction rolled back with no DB writes).{cleanup_note} "
            "Retry when idle."
        ) from exc

    print("\nAPPLIED:")
    for step, count in actual_counts.items():
        print(f"  {step}: {count}")
    print(f"Archive path: {archive_location}")
    print(
        f"Revert command: .venv/bin/python scripts/dalton/ops/merge_duplicate_company_workspace.py "
        f"--revert {archive_location}"
    )
    print(_format_follow_up_notes(), flush=True)

    post_shared_ids: list[str] = []
    post_shared_alarm: str | None = None
    recheck_failed = False
    recheck_error: str | None = None
    if moved_thread_ids:
        try:
            async with store.connect(dsn=dsn, read_only=True) as ro_conn:
                async with ro_conn.cursor(row_factory=dict_row) as ro_cur:
                    await store.set_statement_timeout(
                        ro_cur, POST_COMMIT_RECHECK_TIMEOUT
                    )
                    post_threads = await store.fetch_threads_by_ids(
                        ro_cur, moved_thread_ids
                    )
                await ro_conn.rollback()
            post_shared = [t for t in post_threads if t.get("is_shared") is True]
            if post_shared:
                post_shared_ids = [
                    str(t["conversation_thread_id"]) for t in post_shared
                ]
                post_shared_alarm = _format_post_commit_share_alarm(post_shared)
        except Exception as exc:
            recheck_failed = True
            recheck_error = str(exc)
            post_shared_alarm = _format_post_commit_recheck_failed_alarm(
                canon_ws=canon_norm,
                moved_thread_ids=moved_thread_ids,
                error=exc,
            )

    if post_shared_alarm:
        print("")
        print(post_shared_alarm)
        print(post_shared_alarm, file=sys.stderr)

    return {
        "applied": True,
        "counts": actual_counts,
        "archive_path": archive_location,
        "post_commit_shared_threads": post_shared_ids,
        "post_commit_recheck_failed": recheck_failed,
        "post_commit_recheck_error": recheck_error,
    }


def _verify_archived_threads_match_new(
    archived_threads: list[dict[str, Any]],
    current_threads: list[dict[str, Any]],
) -> None:
    by_id = {str(r["conversation_thread_id"]): r for r in current_threads}
    for rec in archived_threads:
        tid = str(rec["pk"]["conversation_thread_id"])
        new = rec["new"]
        curr = by_id.get(tid)
        if curr is None:
            _refuse(
                f"cannot revert conversation_threads: thread {tid} no longer exists"
            )
        if str(curr.get("workspace_id")) != str(new["workspace_id"]):
            _refuse(
                f"cannot revert conversation_threads: thread {tid} workspace_id changed "
                f"after merge (expected {new['workspace_id']}, got {curr.get('workspace_id')})"
            )
        if int(curr.get("thread_index", -1)) != int(new["thread_index"]):
            _refuse(
                f"cannot revert conversation_threads: thread {tid} thread_index changed "
                f"after merge (expected {new['thread_index']}, got {curr.get('thread_index')})"
            )
        if not _timestamps_equal(curr.get("updated_at"), new.get("updated_at")):
            _refuse(
                f"cannot revert conversation_threads: thread {tid} updated_at changed "
                f"after merge (expected {new.get('updated_at')}, got {curr.get('updated_at')})"
            )


def _verify_archived_sharing_matches_new(
    canon_ws: str,
    canon_sharing_rec: dict[str, Any],
    curr_sharing: dict[str, Any] | None,
) -> None:
    new = canon_sharing_rec["new"]
    if curr_sharing is None:
        _refuse(
            f"cannot revert dalton_workspace_sharing: row for {canon_ws} is missing"
        )
    for field_name in ("cos_access", "web_access", "updated_by"):
        if curr_sharing.get(field_name) != new.get(field_name):
            _refuse(
                f"cannot revert dalton_workspace_sharing: {field_name} changed after "
                f"merge on {canon_ws} (expected {new.get(field_name)!r}, "
                f"got {curr_sharing.get(field_name)!r})"
            )
    if not _timestamps_equal(curr_sharing.get("updated_at"), new.get("updated_at")):
        _refuse(
            f"cannot revert dalton_workspace_sharing: updated_at changed after merge "
            f"on {canon_ws} (expected {new.get('updated_at')}, got {curr_sharing.get('updated_at')})"
        )


def _verify_archived_dup_workspace_matches_new(
    dup_ws: str,
    dup_ws_rec: dict[str, Any],
    curr_dup_ws: dict[str, Any] | None,
) -> None:
    new = dup_ws_rec["new"]
    if curr_dup_ws is None:
        _refuse(f"cannot revert workspaces(dup): workspace {dup_ws} no longer exists")
    if not _configs_equal(curr_dup_ws.get("config"), new.get("config")):
        _refuse(
            f"cannot revert workspaces(dup): config changed after merge on {dup_ws} "
            f"(expected {new.get('config')!r}, got {curr_dup_ws.get('config')!r})"
        )
    if not _timestamps_equal(curr_dup_ws.get("updated_at"), new.get("updated_at")):
        _refuse(
            f"cannot revert workspaces(dup): updated_at changed after merge on {dup_ws} "
            f"(expected {new.get('updated_at')}, got {curr_dup_ws.get('updated_at')})"
        )


async def run_revert(
    *,
    archive_path: Path,
    dsn: str | None = None,
    db: MergeDb | None = None,
) -> dict[str, Any]:
    """Revert a previously applied merge from ``before.jsonl`` in one transaction."""
    store = db or MergeDb()
    records = load_archive_jsonl(archive_path)

    metas = [r for r in records if r["record_type"] == "meta"]
    if len(metas) != 1:
        _refuse(f"expected 1 meta record in {archive_path}, found {len(metas)}")
    meta = metas[0]
    dup_ws = str(meta["dup_ws"])
    canon_ws = str(meta["canon_ws"])
    merge_since = meta.get("tx_now") or meta.get("created_at")
    if not merge_since:
        _refuse(f"archive {archive_path} meta record is missing tx_now/created_at")
    raw_canon_seq = meta.get("canon_max_run_seq")
    if raw_canon_seq is not None and (
        isinstance(raw_canon_seq, bool) or not isinstance(raw_canon_seq, int)
    ):
        _refuse(
            f"archive {archive_path} meta.canon_max_run_seq is invalid: {raw_canon_seq!r}"
        )
    raw_dup_seq = meta.get("dup_max_run_seq")
    if raw_dup_seq is not None and (
        isinstance(raw_dup_seq, bool) or not isinstance(raw_dup_seq, int)
    ):
        _refuse(
            f"archive {archive_path} meta.dup_max_run_seq is invalid: {raw_dup_seq!r}"
        )
    canon_max_run_seq: int | None = raw_canon_seq
    dup_max_run_seq: int | None = raw_dup_seq

    dup_ws_recs = [
        r
        for r in records
        if r["record_type"] == "workspaces" and r.get("role") == "dup"
    ]
    if len(dup_ws_recs) != 1 or "old" not in dup_ws_recs[0] or "new" not in dup_ws_recs[0]:
        _refuse(f"archive {archive_path} is missing dup workspaces pk/old/new record")
    dup_ws_rec = dup_ws_recs[0]

    canon_sharing_recs = [
        r
        for r in records
        if r["record_type"] == "dalton_workspace_sharing" and r.get("role") == "canon"
    ]
    if len(canon_sharing_recs) != 1 or "new" not in canon_sharing_recs[0]:
        _refuse(
            f"archive {archive_path} is missing canon dalton_workspace_sharing new record"
        )
    canon_sharing_rec = canon_sharing_recs[0]

    thread_recs = [
        r for r in records if r["record_type"] == "conversation_threads"
    ]
    for rec in thread_recs:
        if "pk" not in rec or "old" not in rec or "new" not in rec:
            _refuse(f"archive {archive_path} has malformed conversation_threads record")
    thread_ids = [str(r["pk"]["conversation_thread_id"]) for r in thread_recs]

    knowledge_targets = [
        r for r in records if r["record_type"] == "knowledge_plan"
    ]
    for target in knowledge_targets:
        if not target.get("knowledge_version_id") or "target_version_no" not in target:
            _refuse(
                f"archive {archive_path} has knowledge_plan record without "
                "knowledge_version_id / target_version_no"
            )
    knowledge_doc_keys = sorted({str(t["doc_key"]) for t in knowledge_targets})

    meta_counts = meta.get("counts")
    if not isinstance(meta_counts, dict):
        _refuse(f"archive {archive_path} meta record is missing 'counts' mapping")
    actual_archive_counts = {
        "conversation_threads": len(thread_recs),
        "dalton_knowledge_versions": len(knowledge_targets),
        "dalton_workspace_sharing": len(canon_sharing_recs),
        "workspaces_dup_marked": len(dup_ws_recs),
    }
    for count_key, actual_val in actual_archive_counts.items():
        expected_val = meta_counts.get(count_key)
        if isinstance(expected_val, bool) or not isinstance(expected_val, int):
            _refuse(
                f"archive {archive_path} meta.counts[{count_key!r}] is missing or invalid: "
                f"{expected_val!r}"
            )
        if expected_val != actual_val:
            _refuse(
                f"archive {archive_path} record count mismatch for {count_key}: "
                f"meta.counts={expected_val}, found {actual_val} record(s) in archive"
            )

    actual_counts: dict[str, int] = {}
    try:
        async with store.connect(dsn=dsn, read_only=False) as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    # Lock order matching --apply and app paths (sharing before workspaces).
                    await store.set_lock_timeout(cur, LOCK_TIMEOUT)
                    for ws_id in sorted([dup_ws, canon_ws]):
                        await store.lock_thread_index(cur, ws_id)
                    await store.lock_sharing_for_update(cur, canon_ws)
                    await store.lock_workspaces_for_update(cur, [dup_ws, canon_ws])
                    await store.lock_threads_by_ids_for_update(cur, thread_ids)
                    for doc_key in knowledge_doc_keys:
                        await store.lock_knowledge_doc(cur, canon_ws, doc_key)

                    # Pre-verify every mutable row matches its archived 'new' value.
                    curr_threads = await store.fetch_threads_by_ids(cur, thread_ids)
                    _verify_archived_threads_match_new(thread_recs, curr_threads)

                    curr_sharing = await store.fetch_sharing_row(cur, canon_ws)
                    _verify_archived_sharing_matches_new(
                        canon_ws, canon_sharing_rec, curr_sharing
                    )

                    curr_dup_ws = await store.fetch_workspace(cur, dup_ws)
                    _verify_archived_dup_workspace_matches_new(
                        dup_ws, dup_ws_rec, curr_dup_ws
                    )

                    # Revert gate 1: canon has running turns/subagents/commands.
                    canon_activity = await store.fetch_live_activity(cur, canon_ws)
                    if canon_activity.get("busy"):
                        _refuse(
                            f"cannot revert: canon workspace {canon_ws} has live runs "
                            f"in progress (responses={canon_activity.get('live_responses')}, "
                            f"subagent_runs={canon_activity.get('live_subagents')}, "
                            f"command_runs={canon_activity.get('live_commands')}). "
                            "Wait for them to finish and verify workspace state before reverting."
                        )

                    # Revert gate 2: canon has responses newer than recorded DB watermark.
                    post_responses = await store.fetch_canon_responses_after(
                        cur,
                        canon_ws,
                        since_ts=str(merge_since),
                        canon_max_run_seq=canon_max_run_seq,
                        dup_max_run_seq=dup_max_run_seq,
                        migrated_thread_ids=thread_ids,
                    )
                    if post_responses:
                        sample = ", ".join(
                            f"{r['conversation_response_id']}(thread={r['conversation_thread_id']}, "
                            f"run_seq={r.get('run_seq')}, status={r.get('status')}, at={r.get('created_at')})"
                            for r in post_responses[:5]
                        )
                        _refuse(
                            f"cannot revert: canon workspace {canon_ws} has "
                            f"{len(post_responses)} conversation_responses row(s) newer than "
                            f"merge watermark (run_seq>{canon_max_run_seq}, tx_now={merge_since}) "
                            f"[{sample}]. A turn on ANT after merge may have synced merged "
                            f"knowledge to workspace files (data/*.json, *.md, history/, agent.md) "
                            f"or mutated state. Manual fix: inspect ANT workspace files and recent "
                            f"turns against canon_existing in {archive_path}, restore files/state "
                            f"manually if needed, and reconcile before reversing DB rows."
                        )

                    # Revert gate 3: dup has newly created threads after merge.
                    dup_post_threads = await store.fetch_threads_for_workspace(
                        cur, dup_ws
                    )
                    if dup_post_threads:
                        sample_dup = ", ".join(
                            f"{t['conversation_thread_id']}(idx={t.get('thread_index')}, title={t.get('title')!r})"
                            for t in dup_post_threads[:5]
                        )
                        _refuse(
                            f"cannot revert: dup workspace {dup_ws} has "
                            f"{len(dup_post_threads)} thread(s) created after merge "
                            f"[{sample_dup}]; restoring migrated threads would collide on "
                            "thread_index or mix conversations. Archive/remove those threads first."
                        )

                    # Verify knowledge versions are still latest, unwritten, and delete by PK.
                    deleted_kv = await store.verify_and_delete_merged_knowledge_versions(
                        cur, canon_ws, dup_ws, knowledge_targets
                    )
                    _assert_rowcount(
                        "delete dalton_knowledge_versions",
                        deleted_kv,
                        len(knowledge_targets),
                    )
                    actual_counts["deleted_knowledge_versions"] = deleted_kv

                    restored_threads = await store.restore_threads(cur, thread_recs)
                    _assert_rowcount(
                        "restore conversation_threads", restored_threads, len(thread_recs)
                    )
                    actual_counts["restored_conversation_threads"] = restored_threads

                    existed = bool(canon_sharing_rec.get("existed"))
                    old_sharing = canon_sharing_rec.get("old")
                    restored_sharing = await store.restore_sharing(
                        cur, canon_ws, existed, old_sharing
                    )
                    _assert_rowcount("restore dalton_workspace_sharing", restored_sharing, 1)
                    actual_counts["restored_dalton_workspace_sharing"] = restored_sharing

                    dup_restored = await store.restore_dup_workspace_row(
                        cur, dup_ws, dup_ws_rec["old"]
                    )
                    _assert_rowcount("restore workspaces(dup)", dup_restored, 1)
                    actual_counts["restored_workspaces_dup"] = dup_restored
    except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected) as exc:
        if isinstance(exc, psycopg.errors.DeadlockDetected):
            raise MergeRefusedError(
                "REFUSED: deadlock detected during --revert "
                "(another session locked or updated a row in a conflicting order; "
                "database transaction rolled back cleanly with no DB writes). Retry when idle."
            ) from exc
        raise MergeRefusedError(
            f"REFUSED: lock_timeout ({LOCK_TIMEOUT}) exceeded during --revert "
            "(another session holds a lock on dup/canon workspaces, sharing, or "
            "threads; transaction rolled back cleanly). Retry when idle."
        ) from exc

    print("REVERTED:")
    for step, count in actual_counts.items():
        print(f"  {step}: {count}")
    return {"reverted": True, "counts": actual_counts}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Merge a duplicate company workspace into its canonical workspace "
            "(dry-run by default; --apply mutates in one transaction; --revert "
            "restores from before.jsonl)."
        ),
        epilog=(
            "Exit codes:\n"
            "  0  Success (dry-run passed, --apply committed and share recheck passed, or --revert succeeded).\n"
            "  2  Refused or invalid arguments (preflight/revert check failed, archive conflict,\n"
            "     lock_timeout, or deadlock; database rolled back with no writes).\n"
            f"  {EXIT_POST_COMMIT_RECHECK_FAILED}  Database merge COMMITTED, but post-commit share recheck failed\n"
            "     ('分享状态未能复查'); run the printed manual SQL immediately to verify is_shared."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dup", help="Duplicate workspace UUID (source)")
    parser.add_argument("--canon", help="Canonical workspace UUID (destination)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes inside a single Postgres transaction (default: read-only dry run)",
    )
    parser.add_argument(
        "--revert",
        metavar="BEFORE_JSONL",
        help="Revert an applied merge using the exported before.jsonl archive",
    )
    parser.add_argument(
        "--archive-out",
        help="Override before.jsonl output file path when using --apply (must not already exist)",
    )
    parser.add_argument(
        "--dsn",
        help="Optional Postgres DSN (defaults to app DB_* settings)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.revert:
            if args.dup or args.canon or args.apply or args.archive_out:
                parser.error(
                    "--revert cannot be combined with --dup/--canon/--apply/--archive-out"
                )
            asyncio.run(run_revert(archive_path=Path(args.revert).expanduser(), dsn=args.dsn))
            return 0

        if not args.dup or not args.canon:
            parser.error("--dup and --canon are required unless --revert is given")

        result = asyncio.run(
            run_merge(
                dup_ws=args.dup,
                canon_ws=args.canon,
                apply=bool(args.apply),
                archive_out=args.archive_out,
                dsn=args.dsn,
            )
        )
        if result.get("post_commit_recheck_failed"):
            return EXIT_POST_COMMIT_RECHECK_FAILED
        return 0
    except MergeRefusedError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
