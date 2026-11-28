"""Authoritative server-side public share redaction (AU1).

Shared across both Everflow and Standalone Core profiles (``CORE_CAPABILITY``).
Fail-closed everywhere: any permission check failure defaults to
``allow_files=False, allow_download=False``. Redacted content is never logged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from html import escape as html_escape
from html.parser import HTMLParser
import json
import logging
import os
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token
from psycopg.rows import dict_row

from src.server.contracts.turn_kinds import NOTIFICATION_QUERY_TYPES
from src.server.services.history.replay.lines import Line

logger = logging.getLogger(__name__)

DEFAULT_SHARED_TITLE = "Shared Conversation"
PUBLIC_SHARE_MAX_TURNS = 200
PUBLIC_SHARE_MAX_BYTES = 32 * 1024 * 1024
PUBLIC_SHARE_MIN_TURNS = 20

WAKEUP_FIXED_NOTICE = "[定时唤醒触发]"
MARKET_ALERT_FIXED_NOTICE = "[行情提醒触发]"
FAILED_TURN_NOTICE = "*（本轮运行失败，当前分享仅展示正常结束回合的最终回答）*"
CANCELLED_TURN_NOTICE = "*（本轮已中止，当前分享仅展示正常结束回合的最终回答）*"
IN_PROGRESS_TURN_NOTICE = "*（本轮尚未结束，当前分享仅展示正常结束回合的最终回答）*"
TOOLS_ONLY_TURN_NOTICE = "*（本轮仅包含工具操作或文件修改，当前分享已省略执行过程）*"
FORK_SOURCE_UNAVAILABLE_NOTICE = "原对话已删除或不可见，继承的历史消息未在此分享中显示。"

RENDERABLE_FENCE_LANGUAGES: frozenset[str] = frozenset(
    {
        "mermaid",
        "plantuml",
        "graphviz",
        "dot",
        "vega",
        "vega-lite",
        "kroki",
        "markmap",
        "excalidraw",
    }
)

SAFE_NOTIFICATION_FIELD_TUPLE: tuple[str, ...] = (
    "kind",
    "notice",
    "seq",
    "total",
    "last",
    "skipped",
    "fired_at",
    "timezone",
    "target_id",
)

SAFE_WAKEUP_NOTICE_FIELDS: tuple[str, ...] = SAFE_NOTIFICATION_FIELD_TUPLE

SAFE_MARKET_TARGET_FIELDS: tuple[str, ...] = SAFE_NOTIFICATION_FIELD_TUPLE

SAFE_NOTIFICATION_FIELDS: frozenset[str] = frozenset(SAFE_NOTIFICATION_FIELD_TUPLE)

_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {"png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico", "avif"}
)

_STORAGE_SENSITIVE_SEGMENTS: tuple[str, ...] = (
    "response-images",
    "attachments",
    "widgets",
    "chat-uploads",
    "private-media",
    "blobs",
    "uploads",
    "provenance",
    "memo",
    "user-skills",
    "org-library",
    "channel-snapshots",
    "transcriptions",
)

_API_PROTECTED_PREFIXES: tuple[str, ...] = (
    "/api/v1/threads/",
    "/api/v1/public/shared/",
    "/api/v1/workspaces/",
    "/api/v1/wsfiles/",
    "/api/v1/artifacts/",
    "/api/v1/dalton/monitors/g/",
    "/api/v1/dalton/monitors/public/",
    "/api/v1/share-links/",
    "/api/v1/pairings/",
    "/api/v1/attachments",
    "/api/v1/dalton/chat-uploads",
)

_NON_WEB_SCHEMES: frozenset[str] = frozenset(
    {"data", "blob", "file", "javascript", "vbscript"}
)

_CLOUD_PREVIEW_SUFFIXES: tuple[str, ...] = (
    ".e2b.app",
    ".e2b.dev",
    ".daytona.work",
)

_CAPABILITY_HOST_RE = re.compile(r"^\d{1,5}-[a-z2-7]{32}\.", re.IGNORECASE)
_ARTIFACT_HOST_RE = re.compile(
    r"^artifact-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.",
    re.IGNORECASE,
)
_GRANT_PATH_RE = re.compile(r"/g/(?:v1|m1|s1)\.[A-Za-z0-9_.-]+(?:/|$)", re.IGNORECASE)
_STANDALONE_SHORT_LINK_RE = re.compile(r"^/a/[A-Za-z0-9]{12}/?$")
_PURE_FENCE_LANG_RE = re.compile(r"^[A-Za-z0-9_+#.-]{1,32}$")

_FILE_COMMENTS_RE = re.compile(
    r"<\s*file-comments\b[^>]*>[\s\S]*?(?:<\s*/\s*file-comments\s*>|$)",
    re.IGNORECASE,
)
_SYSTEM_REMINDER_RE = re.compile(
    r"<\s*system-reminder\b[^>]*>[\s\S]*?(?:<\s*/\s*system-reminder\s*>|$)",
    re.IGNORECASE,
)

# Standalone backtick-wrapped markdown image or link outside fenced code blocks
_BACKTICK_MD_REF_RE = re.compile(
    r"(?<!`)`(!?\[[^\]\n`]*\]\((?:<[^>\n`]+>|[^)\n`]+)\))`(?!`)"
)

_IMAGE_URL_RE = re.compile(
    r"https?://[^\s\"'<>)\]}（），。；：！？「」『』【】]+\.(?:png|jpe?g|gif|svg|webp|bmp|ico|avif)(?:[?#][^\s\"'<>)\]}（），。；：！？「」『』【】]*)?",
    re.IGNORECASE,
)

_INLINE_URL_RE = re.compile(
    r"(?:https?|file|blob|data|javascript|vbscript):[^\s<>\"'`（）「」『』【】，《》。；：！？]+",
    re.IGNORECASE,
)

_API_REL_URL_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:"
    r"/api/v1/(?:threads|public/shared|workspaces|wsfiles|artifacts|dalton/monitors/(?:g|public)|share-links|pairings|attachments|dalton/chat-uploads)[^\s<>\"'`)\]}（）「」『』【】，《》。；：！？]*"
    r"|__wsref__/[^\s<>\"'`)\]}（）「」『』【】，《》。；：！？]*"
    r"|/?(?:private-media|response-images|attachments|widgets|chat-uploads|blobs|uploads|provenance|memo|user-skills|org-library|channel-snapshots|transcriptions)/[^\s<>\"'`)\]}（）「」『』【】，《》。；：！？]*"
    r")",
    re.IGNORECASE,
)

_BARE_HOST_CRED_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:"
    r"\d{1,5}-[a-z2-7]{32}\.[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s<>\"'`)\]}（）「」『』【】，《》。；：！？]*)?"
    r"|artifact-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s<>\"'`)\]}（）「」『』【】，《》。；：！？]*)?"
    r")",
    re.IGNORECASE,
)

_HOST_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:/Users/[^/\s\"'<>)\]}（）「」『』【】，《》。；：！？]+(?:/[^\s\"'<>)\]}（）「」『』【】，《》。；：！？]*)?"
    r"|/home/(?:workspace|[^/\s\"'<>)\]}（）「」『』【】，《》。；：！？]+)(?:/[^\s\"'<>)\]}（）「」『』【】，《》。；：！？]*)?"
    r"|/mnt/livefs(?:/[^\s\"'<>)\]}（）「」『』【】，《》。；：！？]*)?)"
)

_PATH_SEG_CHAR = r"A-Za-z0-9_.@+\-\u4e00-\u9fff\u3400-\u4dbf"
_COMMON_WS_DIRS = (
    r"reports?|docs?|out|output|outputs|results?|data|datasets?|charts?|figures?|"
    r"images?|imgs?|files?|uploads?|attachments?|artifacts?|workspace|work|tmp|temp|"
    r"scripts?|src|libs?|apps?|web|tests?|notes?|memos?|drafts?|models?|logs?|"
    r"configs?|bin|dist|build|assets?|public|private|skills?|prompts?|templates?|"
    r"migrations?|notebooks?|exports?|downloads?|snapshots?|archive|backups?|"
    r"报告|文档|数据|图表|输出|附件|目录|资料"
)
_KNOWN_NOEXT_FILES = (
    r"README|LICENSE|Makefile|Dockerfile|CHANGELOG|NOTICE|AUTHORS|Procfile|"
    r"Jenkinsfile|Vagrantfile|Caddyfile|Gemfile|Rakefile|Justfile|Taskfile|"
    r"CODEOWNERS|TODO|\.env(?:\.[A-Za-z0-9_.-]+)?|\.gitignore|\.dockerignore"
)
_KNOWN_EXTS = (
    r"pdf|md|markdown|txt|csv|tsv|json|jsonl|ya?ml|toml|ini|cfg|conf|env|xml|html?|css|scss|"
    r"js|jsx|ts|tsx|mjs|cjs|py|pyc|sh|bash|zsh|sql|db|sqlite|parquet|feather|arrow|"
    r"xlsx?|docx?|pptx?|rtf|png|jpe?g|gif|svg|webp|bmp|ico|avif|mp3|mp4|wav|m4a|webm|"
    r"zip|tar|gz|tgz|bz2|7z|rar|log|out|err|tex|bib|r|rmd|ipynb|go|rs|java|c|cc|cpp|h|hpp|rb|php|swift|kt"
)
_DATE_SLASH_RE = re.compile(r"^\d{2,4}/\d{1,2}/\d{1,4}$")

_RELATIVE_WS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.:/@-])(?:"
    rf"\.\.?/[{_PATH_SEG_CHAR}][{_PATH_SEG_CHAR}/]*"
    rf"|(?:[{_PATH_SEG_CHAR}]+/)+[{_PATH_SEG_CHAR}]+\.(?:{_KNOWN_EXTS})(?![A-Za-z0-9_/-])"
    rf"|(?:{_COMMON_WS_DIRS})/(?:[{_PATH_SEG_CHAR}]+/)*[{_PATH_SEG_CHAR}]+(?![A-Za-z0-9_/-])"
    rf"|(?:[{_PATH_SEG_CHAR}]+/)+(?:{_KNOWN_NOEXT_FILES})(?![A-Za-z0-9_/-])"
    rf"|(?:[{_PATH_SEG_CHAR}]+/){{2,}}[{_PATH_SEG_CHAR}]+(?![A-Za-z0-9_/-])"
    r")",
    re.IGNORECASE,
)

_TRAILING_PUNCT = ".,;:!?)]}\"'"

_MD_IMAGE_COUNT_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)|!\[[^\]]*\]\[[^\]]*\]|<\s*(?:img|picture|image|feimage)\b", re.IGNORECASE)

_MAIN_AGENTS = frozenset({None, "", "main"})
_REASONING_CONTENT_TYPE_RE = re.compile(
    r'"content_type"\s*:\s*"(?:reasoning|reasoning_signal)"'
)

_MD_PARSER = (
    MarkdownIt("commonmark", {"html": True})
    .enable("table")
    .enable("strikethrough")
)

# Cold-projection singleflight coordinator: (thread_id, before_turn, lines_epoch) -> Task
_COLD_SINGLEFLIGHT: dict[tuple[str, Any, str], asyncio.Task[Any]] = {}
_BG_WARM_TASKS: set[asyncio.Task[Any]] = set()
_COLD_PROJECTION_HIT: ContextVar[list[bool] | None] = ContextVar(
    "_COLD_PROJECTION_HIT", default=None
)


def mark_cold_projection_miss() -> None:
    """Record that the current replay read ran cold checkpoint projection (cache miss)."""
    flag = _COLD_PROJECTION_HIT.get()
    if flag is not None:
        flag[0] = True


@contextmanager
def track_cold_projection_miss() -> Iterator[list[bool]]:
    """Context manager yielding ``[hit_cold_bool]`` set to True when ``cold.project_stale`` runs."""
    flag = [False]
    token = _COLD_PROJECTION_HIT.set(flag)
    try:
        yield flag
    finally:
        _COLD_PROJECTION_HIT.reset(token)


def normalize_merged_share_permissions(
    existing_perms: Mapping[str, Any] | None,
    incoming_perms: Any | None,
) -> dict[str, Any]:
    """Merge incoming permission fields onto ``existing_perms`` without ever elevating ``allow_files``.

    Unified AU1 rule: whenever effective ``allow_files`` is False, ``allow_download`` is
    forced to False. Partial updates (audience-only, download-only, or omitting ``allow_files``)
    never flip ``allow_files`` from False to True, while submitting ``allow_download=True`` on a
    thread that already has ``allow_files=True`` preserves ``allow_download=True``.
    """
    merged: dict[str, Any] = (
        dict(existing_perms) if isinstance(existing_perms, Mapping) else {}
    )
    if incoming_perms is not None:
        fields_set = getattr(incoming_perms, "model_fields_set", None)
        if fields_set:
            merged.update(incoming_perms.model_dump(exclude_unset=True))
        elif isinstance(incoming_perms, Mapping) and incoming_perms:
            merged.update(incoming_perms)
    if not merged.get("allow_files"):
        merged["allow_files"] = False
        merged["allow_download"] = False
    return merged


def prepare_share_permissions_update(
    existing_perms: Mapping[str, Any] | None,
    incoming_perms: Any | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return ``(effective_perms, atomic_patch)`` for ``POST /threads/{id}/share``.

    ``atomic_patch`` contains ONLY the fields explicitly supplied by the request
    (or ``{}`` when repairing a legacy conflicting row with ``allow_files=False,
    allow_download=True``), so concurrent partial updates merge atomically in SQL
    without overwriting sibling keys changed by another request.
    """
    existing = dict(existing_perms) if isinstance(existing_perms, Mapping) else {}
    fields_set = getattr(incoming_perms, "model_fields_set", None) if incoming_perms is not None else None
    if fields_set:
        patch = dict(incoming_perms.model_dump(exclude_unset=True))
        effective = normalize_merged_share_permissions(existing, patch)
        return effective, patch
    effective = normalize_merged_share_permissions(existing, None)
    if existing.get("allow_download") and not existing.get("allow_files"):
        return effective, {}
    return effective, None


def append_atomic_share_permissions_set(
    sets: list[str],
    params: list[Any],
    patch: Mapping[str, Any],
    json_wrapper: Any,
) -> None:
    """Append an atomic PostgreSQL JSONB merge + normalization clause onto ``sets``/``params``."""
    sets.append(
        "share_permissions = CASE "
        "WHEN COALESCE((COALESCE(share_permissions, '{}'::jsonb) || %s::jsonb)->>'allow_files', 'false') = 'true' "
        "THEN COALESCE(share_permissions, '{}'::jsonb) || %s::jsonb "
        "ELSE (COALESCE(share_permissions, '{}'::jsonb) || %s::jsonb) "
        "|| '{\"allow_files\": false, \"allow_download\": false}'::jsonb "
        "END"
    )
    wrapped = json_wrapper(dict(patch))
    params.extend([wrapped, wrapped, wrapped])


async def atomic_update_thread_sharing(
    conversation_thread_id: str,
    is_shared: bool,
    *,
    share_permissions: Mapping[str, Any] | None = None,
    shared_at: Any | None = None,
    share_permissions_patch: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Dalton-owned atomic JSONB merge UPDATE for ``conversation_threads`` sharing settings.

    Reuses ``mint_share_code`` and returns the exact same ``RETURNING`` columns as
    upstream ``threads_write.update_thread_sharing`` without touching upstream SQL.
    """
    from psycopg.types.json import Json

    from src.server.database import pool
    from src.server.database.share_codes import mint_share_code

    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            sets = ["is_shared = %s", "updated_at = NOW()"]
            params: list[Any] = [is_shared]

            if is_shared:
                sets.append("share_token = COALESCE(share_token, %s)")
                params.append(mint_share_code())

            if share_permissions_patch is not None:
                append_atomic_share_permissions_set(
                    sets, params, share_permissions_patch, Json
                )
            elif share_permissions is not None:
                sets.append("share_permissions = %s")
                params.append(Json(dict(share_permissions)))

            if shared_at is not None:
                sets.append("shared_at = %s")
                params.append(shared_at)

            params.append(conversation_thread_id)

            await cur.execute(
                f"""
                UPDATE conversation_threads
                SET {", ".join(sets)}
                WHERE conversation_thread_id = %s
                RETURNING conversation_thread_id, workspace_id, share_token,
                          is_shared, share_permissions, shared_at,
                          current_status, msg_type, title, created_at, updated_at
                """,
                tuple(params),
            )
            result = await cur.fetchone()
            return dict(result) if result else None


async def apply_thread_share_update(
    update_fn: Any,
    thread_id: str,
    *,
    is_shared: bool,
    shared_at: Any | None,
    effective_perms: Mapping[str, Any],
    patch: Mapping[str, Any] | None,
) -> Any:
    """Update thread sharing using Dalton's atomic SQL JSONB patch merge in production,
    while honoring test monkeypatches on ``sharing.update_thread_sharing``."""
    from src.server.database.conversation.threads_write import (
        update_thread_sharing as upstream_update_thread_sharing,
    )

    if update_fn is upstream_update_thread_sharing:
        return await atomic_update_thread_sharing(
            thread_id,
            is_shared=is_shared,
            shared_at=shared_at,
            share_permissions_patch=dict(patch) if patch is not None else None,
        )

    kwargs: dict[str, Any] = {"is_shared": is_shared}
    if shared_at is not None:
        kwargs["shared_at"] = shared_at
    if patch is not None:
        if getattr(update_fn, "_supports_atomic_share_patch", False):
            kwargs["share_permissions_patch"] = dict(patch)
        else:
            kwargs["share_permissions"] = dict(effective_perms)
    return await update_fn(thread_id, **kwargs)


async def apply_share_update_request(
    thread_id: str,
    existing_perms: Mapping[str, Any] | None,
    effective_req_permissions: Any | None,
    *,
    is_shared: bool,
    shared_at: Any | None,
    x_user_id: str | None,
    require_admin_fn: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int] | None]:
    """Full AU1 orchestrator for ``POST /threads/{id}/share`` (thin-hook target for ``sharing.py``).

    Merges permissions, enforces admin gate for file/download access, runs atomic
    JSONB update, warms slices / computes DB-only summary, and normalizes response
    permissions.
    """
    from fastapi import HTTPException

    from src.server.app.threads import sharing as sharing_app

    effective_perms, perms_patch = prepare_share_permissions_update(
        existing_perms, effective_req_permissions
    )
    if is_shared and (
        effective_perms.get("allow_files") or effective_perms.get("allow_download")
    ):
        await require_admin_fn(x_user_id)

    updated = await apply_thread_share_update(
        sharing_app.update_thread_sharing,
        thread_id,
        is_shared=is_shared,
        shared_at=shared_at,
        effective_perms=effective_perms,
        patch=perms_patch,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Thread not found")

    summary_dict = await post_update_thread_share_hook(
        thread_id, is_shared=bool(updated.get("is_shared"))
    )

    raw_perms = updated.get("share_permissions") or {}
    if isinstance(raw_perms, str):
        raw_perms = json.loads(raw_perms)
    perms = normalize_merged_share_permissions(raw_perms, None)
    return updated, perms, summary_dict


async def maybe_summarize_thread_share_redaction(
    thread_id: str,
    include_summary: Any,
) -> dict[str, int] | None:
    """Return DB-only redaction summary iff ``include_summary`` is strictly ``True``."""
    if include_summary is not True:
        return None
    return await summarize_thread_share_redaction(thread_id)


def redact_rich_steering_wakeup_messages(
    messages: list[Any],
    wakeup_order_keys: frozenset[str],
) -> list[Any]:
    """Replace wakeup steer prompt prose with fixed notice even on ``allow_files=True`` shares."""
    from src.server.services.share_redaction import without_keys

    rebuilt_msgs: list[Any] = []
    for m in messages:
        if isinstance(m, dict) and "wakeup" in m:
            rebuilt_msgs.append(
                {
                    **m,
                    "content": WAKEUP_FIXED_NOTICE,
                    "wakeup": without_keys(m["wakeup"], wakeup_order_keys),
                }
            )
        else:
            rebuilt_msgs.append(m)
    return rebuilt_msgs


@dataclass(frozen=True)
class EffectiveSharePermissions:
    """Authoritative effective permissions for a shared thread."""

    allow_files: bool
    allow_download: bool


def get_public_share_page_budget():
    """Build the default ``PageBudget`` for public share replay."""
    from src.server.services.history.replay import PageBudget

    return PageBudget(
        min_turns=PUBLIC_SHARE_MIN_TURNS,
        max_bytes=PUBLIC_SHARE_MAX_BYTES,
    )


def _parse_perms(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    return raw if isinstance(raw, dict) else {}


async def resolve_effective_share_permissions(
    thread: Mapping[str, Any] | None,
    workspace: Mapping[str, Any] | None = None,
) -> EffectiveSharePermissions:
    """Single authoritative permission resolver for public/org shared threads.

    Fail-closed: any error or missing thread resolves to ``allow_files=False, allow_download=False``.
    """
    if not isinstance(thread, Mapping):
        return EffectiveSharePermissions(allow_files=False, allow_download=False)
    try:
        perms = _parse_perms(thread.get("share_permissions"))
        raw_allow_files = bool(perms.get("allow_files", False))
        if not raw_allow_files:
            return EffectiveSharePermissions(allow_files=False, allow_download=False)

        from src.server.app.share_access import shared_thread_allows_files

        allowed = bool(await shared_thread_allows_files(dict(thread), dict(workspace) if workspace else None))
        allow_files = raw_allow_files and allowed
        allow_download = allow_files and bool(perms.get("allow_download", False))
        return EffectiveSharePermissions(
            allow_files=allow_files,
            allow_download=allow_download,
        )
    except Exception:
        logger.warning(
            "Effective share permission evaluation failed; defaulting to allow_files=False",
            exc_info=True,
        )
        return EffectiveSharePermissions(allow_files=False, allow_download=False)


# =============================================================================
# THINKING MARKER STRIPPING (FULL TEXT, OUTSIDE CODE SPANS/FENCES)
# =============================================================================

_THINK_TAG_TOKEN_RE = re.compile(
    r"<\s*(/?)\s*(thinking_mode|thinking)\s*>",
    re.IGNORECASE,
)
_TRAILING_PARTIAL_THINK_RE = re.compile(
    r"<\s*(?:t(?:h(?:i(?:n(?:k(?:i(?:n(?:g(?:_(?:m(?:o(?:d(?:e)?)?)?)?)?)?)?)?)?)?)?)?)\s*$",
    re.IGNORECASE,
)
_NEED_MORE_THINKING_HEAD_RE = re.compile(
    r"^(?:[ \t]*)(?:need\s+more\s+thinking)\b",
    re.IGNORECASE,
)
_BLANK_LINE_RE = re.compile(r"\n[ \t\r]*\n")


def _strip_thinking_from_plain_chunk(chunk: str, *, is_tail: bool) -> str:
    """Strip closed/nested thinking blocks, unclosed tail tags, and NEED MORE THINKING paragraphs."""
    if not chunk:
        return chunk

    # 1. Strip balanced/nested <thinking_mode>...</thinking_mode> and <thinking>...</thinking>,
    #    plus any unclosed opening tag to end of chunk.
    out_parts: list[str] = []
    pos = 0
    stack: list[str] = []
    n = len(chunk)

    for m in _THINK_TAG_TOKEN_RE.finditer(chunk):
        is_close = bool(m.group(1))
        tag_name = m.group(2).lower()
        if not stack:
            if not is_close:
                out_parts.append(chunk[pos : m.start()])
                stack.append(tag_name)
                pos = m.end()
            else:
                # Stray closing tag: drop it cleanly.
                out_parts.append(chunk[pos : m.start()])
                pos = m.end()
        else:
            if not is_close:
                stack.append(tag_name)
            else:
                # Only pop when closing tag matches the top of the stack.
                if stack and stack[-1] == tag_name:
                    stack.pop()
                if not stack:
                    pos = m.end()

    if not stack and pos < n:
        out_parts.append(chunk[pos:])

    cleaned = "".join(out_parts)

    # 2. Strip truncated partial opening tag at the very end of the final text (e.g. `<thinking`).
    if is_tail and "<" in cleaned[-24:]:
        m_part = _TRAILING_PARTIAL_THINK_RE.search(cleaned)
        if m_part and len(m_part.group(0).strip()) >= 3:
            cleaned = cleaned[: m_part.start()]

    # 3. Strip NEED MORE THINKING paragraphs (at start or after blank line).
    if "need" in cleaned.lower():
        paragraphs = re.split(r"(\n[ \t\r]*\n)", cleaned)
        kept_paras: list[str] = []
        skip_next_sep = False
        for idx, part in enumerate(paragraphs):
            if idx % 2 == 1:
                if skip_next_sep:
                    skip_next_sep = False
                    continue
                kept_paras.append(part)
                continue
            stripped_leading = part.lstrip("\r\n")
            if _NEED_MORE_THINKING_HEAD_RE.match(stripped_leading):
                # Drop preceding blank-line separator if we already emitted one, else drop following sep.
                if kept_paras and _BLANK_LINE_RE.fullmatch(kept_paras[-1]):
                    kept_paras.pop()
                else:
                    skip_next_sep = True
                continue
            kept_paras.append(part)
        cleaned = "".join(kept_paras)

    return cleaned


def _split_code_protected_segments(text: str) -> list[tuple[str, str]]:
    """Split markdown into ``(kind, segment)`` where ``kind`` is ``"plain"``, ``"inline_code"``, or ``"fence"``."""
    from src.dalton.thinking_spans import _skip_fenced_code_block

    segments: list[tuple[str, str]] = []
    buf: list[str] = []
    i = 0
    n = len(text)

    def flush_plain() -> None:
        if buf:
            segments.append(("plain", "".join(buf)))
            buf.clear()

    at_line_start = True
    while i < n:
        # Check fenced code block at line start (including inside ``>`` blockquotes)
        if at_line_start:
            fence_end = _skip_fenced_code_block(text, i)
            if fence_end is not None:
                flush_plain()
                segments.append(("fence", text[i:fence_end]))
                i = fence_end
                at_line_start = True
                continue

        # Check inline code span
        if text[i] == "`":
            k = i
            while k < n and text[k] == "`":
                k += 1
            tick_len = k - i
            # Find matching closing run of exact length tick_len
            search = k
            found_close = -1
            while search < n:
                nxt = text.find("`", search)
                if nxt == -1:
                    break
                end_t = nxt
                while end_t < n and text[end_t] == "`":
                    end_t += 1
                if end_t - nxt == tick_len:
                    found_close = end_t
                    break
                search = end_t
            if found_close != -1:
                flush_plain()
                segments.append(("inline_code", text[i:found_close]))
                at_line_start = text[found_close - 1] == "\n"
                i = found_close
                continue
            buf.append(text[i:k])
            at_line_start = False
            i = k
            continue

        buf.append(text[i])
        at_line_start = text[i] == "\n"
        i += 1

    flush_plain()
    return segments


_NEED_MORE_THINKING_INLINE_START_RE = re.compile(
    r"(?:^|\n)[ \t]*need\s+more\s+thinking\b",
    re.IGNORECASE,
)


def _strip_need_more_thinking_paragraphs(segments: list[tuple[str, str]]) -> str:
    """Strip full ``NEED MORE THINKING`` paragraphs across plain and inline-code segments.

    Fenced code blocks remain protected and delimit paragraphs; inline code spans
    inside a ``NEED MORE THINKING`` paragraph are stripped along with all remaining
    text in that paragraph up to the next blank line or fenced block.
    """
    out: list[str] = []
    in_need_para = False
    at_para_start = True

    for kind, seg in segments:
        if kind == "tag_break":
            # A stripped <thinking> / <thinking_mode> block allows a directly-following
            # `NEED MORE THINKING` paragraph even without an intervening blank line.
            if not in_need_para:
                at_para_start = True
            continue
        if not seg:
            continue
        if kind == "fence":
            in_need_para = False
            out.append(seg)
            at_para_start = True
            continue
        if kind == "inline_code":
            if in_need_para:
                continue
            out.append(seg)
            at_para_start = False
            continue

        parts = re.split(r"(\n[ \t\r]*\n)", seg)
        for idx, part in enumerate(parts):
            if idx % 2 == 1:
                # Blank-line paragraph separator
                if in_need_para:
                    in_need_para = False
                    at_para_start = True
                    if out and not _BLANK_LINE_RE.fullmatch(out[-1]):
                        out.append(part)
                else:
                    if out or part.strip():
                        out.append(part)
                    at_para_start = True
                continue

            if not part:
                continue
            if in_need_para:
                continue

            if at_para_start and _NEED_MORE_THINKING_HEAD_RE.match(part.lstrip("\r\n")):
                in_need_para = True
                at_para_start = False
                if out and _BLANK_LINE_RE.fullmatch(out[-1]):
                    out.pop()
                continue

            # Also handle `NEED MORE THINKING` starting on a new line within a paragraph
            m_line = _NEED_MORE_THINKING_INLINE_START_RE.search(part)
            if m_line is not None:
                prefix = part[: m_line.start()]
                if prefix:
                    out.append(prefix)
                in_need_para = True
                at_para_start = False
                continue

            out.append(part)
            if part.strip():
                at_para_start = False

    # Trim dangling trailing blank-line separator left by stripping the final paragraph
    while out and _BLANK_LINE_RE.fullmatch(out[-1]):
        out.pop()
    return "".join(out)


def strip_inline_thinking_markers(text: str) -> str:
    """Strip all inline thinking markers anywhere in ``text`` outside code blocks/spans."""
    if not text:
        return ""
    # Fast-path check
    low = text.lower()
    if "<" not in low and "need" not in low:
        return text

    segments = _split_code_protected_segments(text)
    plain_indices = [idx for idx, (kind, _) in enumerate(segments) if kind == "plain"]
    last_plain_idx = plain_indices[-1] if plain_indices else -1

    # Pass 1: strip <thinking> / <thinking_mode> blocks & truncated tail tag while
    # preserving code fences and inline code outside thinking blocks.
    after_tags: list[tuple[str, str]] = []
    stack: list[str] = []
    for idx, (kind, seg) in enumerate(segments):
        if kind != "plain":
            if not stack:
                after_tags.append((kind, seg))
            continue
        pos = 0
        for m in _THINK_TAG_TOKEN_RE.finditer(seg):
            is_close = bool(m.group(1))
            tag_name = m.group(2).lower()
            if not stack:
                if not is_close:
                    before = seg[pos : m.start()]
                    if before:
                        after_tags.append(("plain", before))
                    stack.append(tag_name)
                    pos = m.end()
                else:
                    before = seg[pos : m.start()]
                    if before:
                        after_tags.append(("plain", before))
                    pos = m.end()
            else:
                if not is_close:
                    stack.append(tag_name)
                else:
                    if stack and stack[-1] == tag_name:
                        stack.pop()
                    if not stack:
                        after_tags.append(("tag_break", ""))
                        pos = m.end()
        if not stack and pos < len(seg):
            tail_seg = seg[pos:]
            if idx == last_plain_idx and "<" in tail_seg[-24:]:
                m_part = _TRAILING_PARTIAL_THINK_RE.search(tail_seg)
                if m_part and len(m_part.group(0).strip()) >= 3:
                    tail_seg = tail_seg[: m_part.start()]
            if tail_seg:
                after_tags.append(("plain", tail_seg))

    # Pass 2: strip full NEED MORE THINKING paragraphs across plain + inline-code spans.
    result = _strip_need_more_thinking_paragraphs(after_tags)
    return result.strip()


# =============================================================================
# PROTECTED REFERENCE DETECTION & SCRUBBING
# =============================================================================

def _configured_storage_prefixes() -> tuple[str, ...]:
    prefixes: list[str] = []
    for env_name in (
        "STORAGE_KEY_PREFIX",
        "DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX",
        "DALTON_ASR_STORAGE_KEY_PREFIX",
    ):
        raw = (os.getenv(env_name) or "").strip().strip("/")
        if raw and raw not in prefixes:
            prefixes.append(raw)
    return tuple(prefixes)


def _normalize_url_for_check(raw: str) -> tuple[str, str, str, str]:
    """Return ``(normalized_url, scheme, host, decoded_path)``."""
    cleaned = (raw or "").strip()
    try:
        parts = urlsplit(cleaned)
    except Exception:
        return cleaned.lower(), "", "", cleaned.lower()
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        port = parts.port
    except ValueError:
        port = None
    default_port = 443 if scheme == "https" else (80 if scheme == "http" else None)
    netloc = host
    if port is not None and port != default_port:
        netloc = f"{host}:{port}"
    raw_path = parts.path or ""
    # Double-unquote to defeat percent-encoding tricks (%67 -> g, %2567 -> g)
    decoded_path = unquote(unquote(raw_path))
    norm_url = f"{scheme}://{netloc}{decoded_path}" if scheme and netloc else decoded_path
    if parts.query:
        norm_url = f"{norm_url}?{parts.query}"
    return norm_url, scheme, host, decoded_path


def _is_standalone_short_link(scheme: str, decoded_path: str) -> bool:
    if scheme not in ("", "http", "https"):
        return False
    return bool(_STANDALONE_SHORT_LINK_RE.match(decoded_path))


def _has_image_extension(decoded_path: str) -> bool:
    leaf = decoded_path.rsplit("/", 1)[-1]
    if "." not in leaf:
        return False
    ext = leaf.rsplit(".", 1)[-1].lower()
    return ext in _IMAGE_EXTENSIONS


def _is_preview_or_artifact_host(host: str) -> bool:
    if not host:
        return False
    if _CAPABILITY_HOST_RE.match(host) or _ARTIFACT_HOST_RE.match(host):
        return True
    if any(host.endswith(suffix) for suffix in _CLOUD_PREVIEW_SUFFIXES):
        return True
    try:
        from src.server.services.preview_base import artifact_base, preview_base

        for base in (preview_base(), artifact_base()):
            if base is not None and (
                base.claims(host)
                or base.parse(host) is not None
                or base.parse_artifact(host) is not None
            ):
                return True
    except Exception:
        pass
    return False


def _path_has_sensitive_storage_segment(decoded_path: str) -> bool:
    low = decoded_path.lower()
    # Check both leading-slash and relative prefix forms
    trimmed = low.lstrip("/")
    for seg in _STORAGE_SENSITIVE_SEGMENTS:
        if f"/{seg}/" in low or trimmed.startswith(f"{seg}/"):
            return True
    for cfg_prefix in _configured_storage_prefixes():
        cp = cfg_prefix.lower()
        if trimmed.startswith(f"{cp}/"):
            rest = trimmed[len(cp) + 1 :]
            for seg in _STORAGE_SENSITIVE_SEGMENTS:
                if rest.startswith(f"{seg}/"):
                    return True
    return False


def is_protected_reference(
    raw: str,
    *,
    protected_urls: Iterable[str] = (),
    roots: Iterable[str] = (),
    treat_image_url_as_protected: bool = False,
) -> bool:
    """Return True if ``raw`` is a protected URL, grant/preview credential, storage path, or host/workspace path."""
    candidate = (raw or "").strip()
    if not candidate:
        return False

    # Bare capability or artifact host without scheme
    low_cand = candidate.lower()
    if _CAPABILITY_HOST_RE.match(low_cand) or _ARTIFACT_HOST_RE.match(low_cand):
        return True

    norm_url, scheme, host, decoded_path = _normalize_url_for_check(candidate)
    low_path = decoded_path.lower()

    # Preserve standalone /a/{12-char-code} published short links unless they carry a grant or host credential
    if (
        _is_standalone_short_link(scheme, decoded_path)
        and not _is_preview_or_artifact_host(host)
        and "/g/" not in low_path
    ):
        return False

    if scheme in _NON_WEB_SCHEMES:
        return True

    if "__wsref__/" in low_cand or "__wsref__/" in low_path:
        return True

    if any(low_path.startswith(prefix) for prefix in _API_PROTECTED_PREFIXES):
        return True

    if _is_preview_or_artifact_host(host):
        return True

    if _GRANT_PATH_RE.search(decoded_path):
        return True

    if _path_has_sensitive_storage_segment(decoded_path):
        return True

    if treat_image_url_as_protected and _has_image_extension(decoded_path):
        return True

    # Thread-registered protected URLs
    for purl in protected_urls:
        if not purl:
            continue
        pnorm, _, phost, ppath = _normalize_url_for_check(str(purl))
        if pnorm and (norm_url == pnorm or norm_url.startswith(pnorm.rstrip("/") + "/")):
            return True
        if phost and host == phost and ppath and ppath != "/" and decoded_path == ppath:
            return True

    # Host / sandbox filesystem roots
    if low_path.startswith(("/users/", "/home/", "/mnt/livefs")):
        return True
    for root in roots:
        r = str(root or "").strip().rstrip("/")
        if r and (candidate.startswith(r) or decoded_path.startswith(r)):
            return True

    # Non-http(s)/mailto relative workspace paths (e.g. `out/report.md`, `charts/a.png`)
    if not scheme:
        if candidate.startswith("#"):
            return False
        return True

    if scheme not in ("http", "https", "mailto"):
        return True

    return False


def _split_trailing_punct(token: str) -> tuple[str, str]:
    """Detach trailing ASCII punctuation while preserving balanced parentheses inside URLs."""
    core = token
    tail = ""
    while core:
        ch = core[-1]
        if ch in ".,;:!?'\"":
            tail = ch + tail
            core = core[:-1]
        elif ch == ")" and core.count("(") < core.count(")"):
            tail = ch + tail
            core = core[:-1]
        elif ch == "]" and core.count("[") < core.count("]"):
            tail = ch + tail
            core = core[:-1]
        else:
            break
    return core, tail


def scrub_protected_references(
    text: str,
    *,
    protected_urls: Iterable[str] = (),
    roots: Iterable[str] = (),
    scrub_all_image_urls: bool = False,
) -> str:
    """Replace all protected URLs, grant/preview credentials, and host/workspace paths in ``text``."""
    if not text:
        return text

    p_urls = tuple(str(u) for u in protected_urls if u)
    norm_roots = sorted(
        {str(r).strip().rstrip("/") for r in roots if str(r or "").strip()},
        key=len,
        reverse=True,
    )

    def _replace_url(m: re.Match[str]) -> str:
        core, tail = _split_trailing_punct(m.group(0))
        if is_protected_reference(
            core,
            protected_urls=p_urls,
            roots=norm_roots,
            treat_image_url_as_protected=scrub_all_image_urls,
        ):
            return "[redacted-url]" + tail
        return m.group(0)

    res = _INLINE_URL_RE.sub(_replace_url, text)
    res = _API_REL_URL_RE.sub(lambda m: _replace_rel(m), res)
    res = _BARE_HOST_CRED_RE.sub(_replace_url, res)

    if scrub_all_image_urls:
        res = _IMAGE_URL_RE.sub("[redacted-image-url]", res)

    # Explicit root paths first (longest first)
    for root in norm_roots:
        if root in res:
            pattern = re.compile(
                re.escape(root) + r"(?:/[^\s\"'<>)\]}（）「」『』【】，《》。；：！？]*)?"
            )
            res = pattern.sub("[redacted-path]", res)

    # Generic host/sandbox paths
    res = _HOST_PATH_RE.sub(lambda m: _replace_host_path(m), res)

    # Workspace-relative paths (e.g. `reports/q3.pdf`, `out/memo.md`, `./charts/a.png`),
    # while leaving intact any allowed non-protected http(s):// URLs.
    protected_url_spans: list[tuple[int, int]] = [
        (m.start(), m.end()) for m in _INLINE_URL_RE.finditer(res)
    ]

    def _replace_rel_ws_path(m: re.Match[str]) -> str:
        s, e = m.start(), m.end()
        for us, ue in protected_url_spans:
            if s >= us and e <= ue:
                return m.group(0)
        core, tail = _split_trailing_punct(m.group(0))
        if not core or _DATE_SLASH_RE.fullmatch(core):
            return m.group(0)
        return "[redacted-path]" + tail

    res = _RELATIVE_WS_PATH_RE.sub(_replace_rel_ws_path, res)
    return res


def _replace_rel(m: re.Match[str]) -> str:
    core, tail = _split_trailing_punct(m.group(0))
    if _STANDALONE_SHORT_LINK_RE.match(core):
        return m.group(0)
    return "[redacted-url]" + tail


def _replace_host_path(m: re.Match[str]) -> str:
    core, tail = _split_trailing_punct(m.group(0))
    if not core:
        return m.group(0)
    return "[redacted-path]" + tail


# =============================================================================
# HTML WHITELIST RECONSTRUCTOR
# =============================================================================

_HTML_IMAGE_TAGS = frozenset({"img", "image", "feimage"})
_HTML_DROP_WITH_CONTENT_TAGS = frozenset({"script", "style"})
_HTML_DROP_CONTAINER_TAGS = frozenset(
    {
        "video",
        "audio",
        "source",
        "track",
        "iframe",
        "frame",
        "embed",
        "object",
        "applet",
        "portal",
        "link",
        "base",
        "meta",
        "form",
        "use",
        "foreignobject",
    }
)
_HTML_SAFE_FORMATTING_TAGS = frozenset(
    {
        "p",
        "br",
        "hr",
        "strong",
        "em",
        "del",
        "blockquote",
        "ul",
        "ol",
        "li",
        "table",
        "thead",
        "tbody",
        "tr",
        "pre",
        "code",
        "sup",
        "sub",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)
_HTML_VOID_TAGS = frozenset({"br", "hr"})
_HTML_CELL_TAGS = frozenset({"th", "td"})
_HTML_ALIGN_VALUES = frozenset({"left", "center", "right"})


def _make_safe_alt(
    raw_alt: str,
    *,
    protected_urls: tuple[str, ...],
    roots: tuple[str, ...],
) -> str:
    cleaned = (raw_alt or "").strip()
    if not cleaned:
        return ""
    if (
        _INLINE_URL_RE.search(cleaned)
        or _BARE_HOST_CRED_RE.search(cleaned)
        or _API_REL_URL_RE.search(cleaned)
        or _has_image_extension(cleaned)
        or "/" in cleaned
        or "\\" in cleaned
        or "__wsref__" in cleaned.lower()
    ):
        if is_protected_reference(
            cleaned,
            protected_urls=protected_urls,
            roots=roots,
            treat_image_url_as_protected=True,
        ):
            return ""
    scrubbed = scrub_protected_references(
        cleaned,
        protected_urls=protected_urls,
        roots=roots,
        scrub_all_image_urls=True,
    ).strip()
    # Also strip any remaining generic http(s) URLs from alt text
    scrubbed = _INLINE_URL_RE.sub("", scrubbed).strip()
    if scrubbed in ("", "[redacted-url]", "[redacted-path]", "[redacted-image-url]"):
        return ""
    escaped = html_escape(scrubbed, quote=True).replace("$", "&#36;")
    return escaped


def _format_redacted_image(
    raw_alt: str,
    *,
    protected_urls: tuple[str, ...],
    roots: tuple[str, ...],
) -> str:
    safe_alt = _make_safe_alt(raw_alt, protected_urls=protected_urls, roots=roots)
    return f'<redacted-image alt="{safe_alt}"></redacted-image>'


class _SafeHtmlReconstructor(HTMLParser):
    """Strict whitelist HTML reconstructor for raw HTML blocks/inlines in shared markdown."""

    def __init__(
        self,
        *,
        protected_urls: tuple[str, ...],
        roots: tuple[str, ...],
    ) -> None:
        super().__init__(convert_charrefs=True)
        self._protected_urls = protected_urls
        self._roots = roots
        self._out: list[str] = []
        self._drop_content_depth = 0
        self._picture_depth = 0
        self._picture_emitted = False
        self._svg_depth = 0
        self._svg_emitted = False
        self._a_stack: list[tuple[bool, list[str]]] = []

    def _emit(self, chunk: str) -> None:
        if self._a_stack:
            self._a_stack[-1][1].append(chunk)
        else:
            self._out.append(chunk)

    @property
    def has_open_context(self) -> bool:
        return bool(
            self._a_stack
            or self._drop_content_depth
            or self._picture_depth
            or self._svg_depth
        )

    def drain(self) -> str:
        if not self._out:
            return ""
        chunk = "".join(self._out)
        self._out.clear()
        return chunk

    def emit_rendered_inline(self, rendered: str) -> None:
        if self._drop_content_depth > 0 or not rendered:
            return
        self._emit(rendered)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        if self._drop_content_depth > 0:
            if t in _HTML_DROP_WITH_CONTENT_TAGS:
                self._drop_content_depth += 1
            return

        if t in _HTML_DROP_WITH_CONTENT_TAGS:
            self._drop_content_depth += 1
            return

        if t == "redacted-image":
            self._emit(
                _format_redacted_image(
                    attr_map.get("alt", ""),
                    protected_urls=self._protected_urls,
                    roots=self._roots,
                )
            )
            return

        if t == "picture":
            self._picture_depth += 1
            self._picture_emitted = False
            return

        if t == "svg":
            self._svg_depth += 1
            self._svg_emitted = False
            return

        if t in _HTML_IMAGE_TAGS or (
            t == "input" and attr_map.get("type", "").strip().lower() == "image"
        ):
            if self._picture_depth > 0:
                if not self._picture_emitted:
                    self._emit(
                        _format_redacted_image(
                            attr_map.get("alt", ""),
                            protected_urls=self._protected_urls,
                            roots=self._roots,
                        )
                    )
                    self._picture_emitted = True
                return
            if self._svg_depth > 0:
                if not self._svg_emitted:
                    self._emit(
                        _format_redacted_image(
                            attr_map.get("alt", ""),
                            protected_urls=self._protected_urls,
                            roots=self._roots,
                        )
                    )
                    self._svg_emitted = True
                return
            self._emit(
                _format_redacted_image(
                    attr_map.get("alt", ""),
                    protected_urls=self._protected_urls,
                    roots=self._roots,
                )
            )
            return

        if t in _HTML_DROP_CONTAINER_TAGS:
            return

        if t == "a":
            href = attr_map.get("href", "").strip()
            allowed_href = self._safe_public_href(href)
            if allowed_href:
                self._emit(f'<a href="{html_escape(allowed_href, quote=True)}">')
                self._a_stack.append((True, []))
            else:
                self._a_stack.append((False, []))
            return

        if t in _HTML_SAFE_FORMATTING_TAGS:
            self._emit(f"<{t}>")
            return

        if t in _HTML_CELL_TAGS:
            safe_attrs: list[str] = []
            for int_attr in ("colspan", "rowspan"):
                val = attr_map.get(int_attr, "").strip()
                if val.isdigit():
                    safe_attrs.append(f'{int_attr}="{val}"')
            align = attr_map.get("align", "").strip().lower()
            if align in _HTML_ALIGN_VALUES:
                safe_attrs.append(f'align="{align}"')
            attr_str = (" " + " ".join(safe_attrs)) if safe_attrs else ""
            self._emit(f"<{t}{attr_str}>")
            return

        # Any other unknown tag (e.g. span/div/font): drop tag & all attributes, keep inner text.

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if self._drop_content_depth > 0:
            if t in _HTML_DROP_WITH_CONTENT_TAGS:
                self._drop_content_depth -= 1
            return

        if t == "picture":
            if self._picture_depth > 0:
                if not self._picture_emitted:
                    self._emit(
                        _format_redacted_image(
                            "",
                            protected_urls=self._protected_urls,
                            roots=self._roots,
                        )
                    )
                    self._picture_emitted = True
                self._picture_depth -= 1
            return

        if t == "svg":
            if self._svg_depth > 0:
                self._svg_depth -= 1
            return

        if t == "a":
            if self._a_stack:
                kept, buf = self._a_stack.pop()
                inner = "".join(buf)
                if kept:
                    self._emit(inner + "</a>")
                else:
                    scrubbed = scrub_protected_references(
                        inner,
                        protected_urls=self._protected_urls,
                        roots=self._roots,
                        scrub_all_image_urls=True,
                    ).strip()
                    if scrubbed in ("", "[redacted-url]", "[redacted-path]", "[redacted-image-url]"):
                        self._emit("[已隐藏链接]")
                    else:
                        self._emit(scrubbed)
            return

        if t in _HTML_SAFE_FORMATTING_TAGS and t not in _HTML_VOID_TAGS:
            self._emit(f"</{t}>")
            return

        if t in _HTML_CELL_TAGS:
            self._emit(f"</{t}>")
            return

    def handle_data(self, data: str) -> None:
        if self._drop_content_depth > 0:
            return
        scrubbed = scrub_protected_references(
            data,
            protected_urls=self._protected_urls,
            roots=self._roots,
        )
        self._emit(html_escape(scrubbed, quote=False))

    def _safe_public_href(self, href: str) -> str | None:
        if not href:
            return None
        if is_protected_reference(
            href,
            protected_urls=self._protected_urls,
            roots=self._roots,
            treat_image_url_as_protected=True,
        ):
            return None
        _, scheme, _, decoded_path = _normalize_url_for_check(href)
        if scheme in ("http", "https", "mailto"):
            return href
        if scheme == "" and _STANDALONE_SHORT_LINK_RE.match(decoded_path):
            return href
        return None

    def get_result(self) -> str:
        # Flush any unclosed <a> tags
        while self._a_stack:
            kept, buf = self._a_stack.pop()
            inner = "".join(buf)
            if kept:
                self._out.append(inner + "</a>")
            else:
                self._out.append(inner or "[已隐藏链接]")
        return "".join(self._out)


def _sanitize_html_fragment(
    raw_html: str,
    *,
    protected_urls: tuple[str, ...],
    roots: tuple[str, ...],
) -> str:
    parser = _SafeHtmlReconstructor(protected_urls=protected_urls, roots=roots)
    parser.feed(raw_html)
    parser.close()
    return parser.get_result()


# =============================================================================
# MARKDOWN AST RECONSTRUCTION SANITIZER
# =============================================================================

def _unwrap_backtick_file_refs(text: str) -> str:
    """Align with frontend ``normalizeFileRefs.ts`` outside fenced code blocks."""
    segments = _split_code_protected_segments(text)
    out: list[str] = []
    for is_code, seg in segments:
        if is_code:
            # Only unwrap standalone single-backtick markdown image/link expressions, never fenced blocks
            if seg.startswith("`") and not seg.startswith("``") and "\n" not in seg:
                m = _BACKTICK_MD_REF_RE.fullmatch(seg)
                if m:
                    inner = m.group(1)
                    # Strip file:// prefix & wrap space-containing destinations in <...>
                    inner = _normalize_md_dest(inner)
                    out.append(inner)
                    continue
            out.append(seg)
        else:
            out.append(seg)
    return "".join(out)


def _normalize_md_dest(expr: str) -> str:
    idx = expr.find("](")
    if idx == -1 or not expr.endswith(")"):
        return expr
    head = expr[: idx + 2]
    dest = expr[idx + 2 : -1].strip()
    if dest.startswith("<") and dest.endswith(">"):
        inner = dest[1:-1]
    else:
        inner = dest
    if inner.lower().startswith("file://"):
        inner = inner[7:]
    if " " in inner and not (dest.startswith("<") and dest.endswith(">")):
        return f"{head}<{inner}>)"
    return f"{head}{inner})"


def _fence_backticks_for(body: str) -> str:
    longest = 0
    for m in re.finditer(r"`+", body):
        if len(m.group(0)) > longest:
            longest = len(m.group(0))
    return "`" * max(3, longest + 1)


def _inline_code_wrap(code: str) -> str:
    longest = 0
    for m in re.finditer(r"`+", code):
        if len(m.group(0)) > longest:
            longest = len(m.group(0))
    ticks = "`" * (longest + 1)
    if code.startswith("`") or code.endswith("`") or (code.startswith(" ") and code.endswith(" ") and code.strip()):
        return f"{ticks} {code} {ticks}"
    return f"{ticks}{code}{ticks}"


def _scrub_renderable_fence_body(
    body: str,
    *,
    protected_urls: tuple[str, ...],
    roots: tuple[str, ...],
) -> str:
    """Scrub image tags/URLs and protected references inside downgraded mermaid/diagram fences."""
    # Replace HTML <img ...> / <image ...> tags inside diagram node labels
    cleaned = re.sub(
        r"<\s*(?:img|image|feimage)\b[^>]*>",
        "[redacted-image-url]",
        body,
        flags=re.IGNORECASE,
    )
    cleaned = _IMAGE_URL_RE.sub("[redacted-image-url]", cleaned)
    cleaned = scrub_protected_references(
        cleaned,
        protected_urls=protected_urls,
        roots=roots,
        scrub_all_image_urls=True,
    )
    return cleaned


_SAFE_PLACEHOLDER_TOKENS = frozenset(
    {"[redacted-url]", "[redacted-path]", "[redacted-image-url]", "[已隐藏链接]"}
)
_SAFE_PLACEHOLDER_SPLIT_RE = re.compile(
    r"(\[redacted-url\]|\[redacted-path\]|\[redacted-image-url\]|\[已隐藏链接\])"
)


def _escape_markdown_text_chunk(raw: str) -> str:
    """Escape Markdown/HTML syntax characters in AST-decoded text tokens so
    re-serialization can never turn escaped images/links/HTML back into active nodes."""
    if not raw:
        return raw
    parts = _SAFE_PLACEHOLDER_SPLIT_RE.split(raw)
    escaped_parts: list[str] = []
    for part in parts:
        if not part:
            continue
        if part in _SAFE_PLACEHOLDER_TOKENS:
            escaped_parts.append(part)
            continue
        chunk = (
            part.replace("\\", "\\\\")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("`", "\\`")
            .replace("[", "\\[")
            .replace("]", "\\]")
        )
        # Escape `!` when preceding `[` (now `\[`) or at end of text token (cross-token `![`)
        chunk = chunk.replace("!\\[", "\\!\\[")
        if chunk.endswith("!"):
            chunk = chunk[:-1] + "\\!"
        # Prevent `[redacted-url]` immediately followed by `(href)` from forming a link
        if escaped_parts and escaped_parts[-1] in _SAFE_PLACEHOLDER_TOKENS:
            if chunk.startswith("("):
                chunk = "\\" + chunk
        escaped_parts.append(chunk)
    return "".join(escaped_parts)


_AST_ALLOWED_HTML_TAGS = (
    _HTML_SAFE_FORMATTING_TAGS | _HTML_CELL_TAGS | {"a", "redacted-image"}
)
_AST_HTML_TAG_RE = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9:-]*)\b([^>]*)>")


def _has_disallowed_ast_resource_nodes(
    markdown: str,
    *,
    protected_urls: tuple[str, ...],
    roots: tuple[str, ...],
) -> bool:
    """Re-parse serialized Markdown and assert no unauthorized resource/HTML nodes exist."""
    tokens = _MD_PARSER.parse(markdown)
    stack: list[Token] = list(tokens)
    while stack:
        tok = stack.pop()
        if tok.children:
            stack.extend(tok.children)
        ttype = tok.type
        if ttype == "image":
            return True
        if ttype == "link_open":
            href = str(tok.attrGet("href") or "").strip()
            if not href or is_protected_reference(
                href,
                protected_urls=protected_urls,
                roots=roots,
                treat_image_url_as_protected=True,
            ):
                return True
            _, scheme, _, decoded_path = _normalize_url_for_check(href)
            if not (
                scheme in ("http", "https", "mailto")
                or (scheme == "" and _STANDALONE_SHORT_LINK_RE.match(decoded_path))
            ):
                return True
        if ttype in ("html_inline", "html_block"):
            frag = tok.content.strip()
            if not frag:
                continue
            tags = list(_AST_HTML_TAG_RE.finditer(frag))
            if not tags:
                return True
            for m in tags:
                is_close = bool(m.group(1))
                tag = m.group(2).lower()
                attrs = m.group(3) or ""
                if tag not in _AST_ALLOWED_HTML_TAGS:
                    return True
                if is_close:
                    continue
                low_attrs = attrs.lower()
                if re.search(
                    r"\b(?:src|srcset|imagesrcset|poster|background|xlink:href|style|title|aria-[a-z0-9_-]+|data-[a-z0-9_-]+|on[a-z]+)\s*=",
                    low_attrs,
                ):
                    return True
                if tag == "a":
                    href_m = re.search(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", attrs, re.IGNORECASE)
                    href_val = (href_m.group(1) or href_m.group(2) or href_m.group(3) or "").strip() if href_m else ""
                    if href_val and is_protected_reference(
                        href_val,
                        protected_urls=protected_urls,
                        roots=roots,
                        treat_image_url_as_protected=True,
                    ):
                        return True
    return False


def _render_inline_tokens(
    children: list[Token] | None,
    *,
    protected_urls: tuple[str, ...],
    roots: tuple[str, ...],
) -> str:
    if not children:
        return ""
    out: list[str] = []
    html_parser: _SafeHtmlReconstructor | None = None
    i = 0
    n = len(children)

    def _push(chunk: str) -> None:
        if html_parser is not None and html_parser.has_open_context:
            html_parser.emit_rendered_inline(chunk)
        else:
            out.append(chunk)

    while i < n:
        tok = children[i]
        ttype = tok.type

        if ttype == "text":
            scrubbed_text = scrub_protected_references(
                tok.content,
                protected_urls=protected_urls,
                roots=roots,
                scrub_all_image_urls=True,
            )
            _push(_escape_markdown_text_chunk(scrubbed_text))
        elif ttype == "softbreak":
            _push("\n")
        elif ttype == "hardbreak":
            _push("  \n")
        elif ttype == "code_inline":
            scrubbed = scrub_protected_references(
                tok.content, protected_urls=protected_urls, roots=roots
            )
            _push(_inline_code_wrap(scrubbed))
        elif ttype == "html_inline":
            if html_parser is None:
                html_parser = _SafeHtmlReconstructor(
                    protected_urls=protected_urls, roots=roots
                )
            html_parser.feed(tok.content)
            if not html_parser.has_open_context:
                flushed = html_parser.drain()
                if flushed:
                    out.append(flushed)
        elif ttype == "image":
            alt_text = tok.content
            if tok.children:
                alt_text = "".join(c.content for c in tok.children if c.content)
            _push(
                _format_redacted_image(
                    alt_text, protected_urls=protected_urls, roots=roots
                )
            )
        elif ttype == "link_open":
            href = str(tok.attrGet("href") or "").strip()
            # Find matching link_close
            depth = 1
            j = i + 1
            inner_tokens: list[Token] = []
            while j < n and depth > 0:
                if children[j].type == "link_open":
                    depth += 1
                elif children[j].type == "link_close":
                    depth -= 1
                    if depth == 0:
                        break
                inner_tokens.append(children[j])
                j += 1
            label_rendered = _render_inline_tokens(
                inner_tokens, protected_urls=protected_urls, roots=roots
            )
            # Validate href (drops title completely; converts reference links to inline links)
            is_protected = is_protected_reference(
                href,
                protected_urls=protected_urls,
                roots=roots,
                treat_image_url_as_protected=True,
            )
            _, scheme, _, decoded_path = _normalize_url_for_check(href)
            is_allowed_link = (
                bool(href)
                and not is_protected
                and (
                    scheme in ("http", "https", "mailto")
                    or (scheme == "" and _STANDALONE_SHORT_LINK_RE.match(decoded_path))
                )
            )
            if is_allowed_link:
                safe_label = label_rendered or href
                safe_dest = f"<{href}>" if (" " in href or ")" in href) else href
                _push(f"[{safe_label}]({safe_dest})")
            else:
                plain = label_rendered.strip()
                if "<redacted-image" in plain:
                    _push(plain)
                elif plain in ("", "[redacted-url]", "[redacted-path]", "[redacted-image-url]"):
                    _push("[已隐藏链接]")
                else:
                    _push(plain)
            i = j + 1
            continue
        elif ttype == "strong_open":
            _push("**")
        elif ttype == "strong_close":
            _push("**")
        elif ttype == "em_open":
            _push("*")
        elif ttype == "em_close":
            _push("*")
        elif ttype == "s_open":
            _push("~~")
        elif ttype == "s_close":
            _push("~~")

        i += 1

    if html_parser is not None:
        html_parser.close()
        tail = html_parser.get_result()
        if tail:
            out.append(tail)

    return "".join(out)


def _render_block_tokens(
    tokens: list[Token],
    *,
    protected_urls: tuple[str, ...],
    roots: tuple[str, ...],
) -> str:
    blocks: list[str] = []
    i = 0
    n = len(tokens)

    while i < n:
        tok = tokens[i]
        ttype = tok.type

        if ttype == "heading_open":
            level = int(tok.tag[1]) if len(tok.tag) == 2 and tok.tag[1].isdigit() else 1
            inline_tok = tokens[i + 1] if i + 1 < n and tokens[i + 1].type == "inline" else None
            rendered = _render_inline_tokens(
                inline_tok.children if inline_tok else None,
                protected_urls=protected_urls,
                roots=roots,
            )
            blocks.append(f"{'#' * level} {rendered}".rstrip())
            # Advance past heading_close
            while i < n and tokens[i].type != "heading_close":
                i += 1
            i += 1
            continue

        if ttype == "paragraph_open":
            inline_tok = tokens[i + 1] if i + 1 < n and tokens[i + 1].type == "inline" else None
            rendered = _render_inline_tokens(
                inline_tok.children if inline_tok else None,
                protected_urls=protected_urls,
                roots=roots,
            )
            if rendered:
                blocks.append(rendered)
            while i < n and tokens[i].type != "paragraph_close":
                i += 1
            i += 1
            continue

        if ttype == "fence":
            raw_info = (tok.info or "").strip()
            first_word = raw_info.split(None, 1)[0] if raw_info else ""
            lang = first_word if _PURE_FENCE_LANG_RE.match(first_word) else ""
            body = tok.content
            if lang.lower() in RENDERABLE_FENCE_LANGUAGES:
                lang = "text"
                body = _scrub_renderable_fence_body(
                    body, protected_urls=protected_urls, roots=roots
                )
            else:
                body = scrub_protected_references(
                    body, protected_urls=protected_urls, roots=roots
                )
            fence = _fence_backticks_for(body)
            body_clean = body.rstrip("\n")
            blocks.append(f"{fence}{lang}\n{body_clean}\n{fence}")
            i += 1
            continue

        if ttype == "code_block":
            body = scrub_protected_references(
                tok.content, protected_urls=protected_urls, roots=roots
            )
            fence = _fence_backticks_for(body)
            blocks.append(f"{fence}text\n{body.rstrip(chr(10))}\n{fence}")
            i += 1
            continue

        if ttype == "html_block":
            sanitized = _sanitize_html_fragment(
                tok.content, protected_urls=protected_urls, roots=roots
            ).strip()
            if sanitized:
                blocks.append(sanitized)
            i += 1
            continue

        if ttype == "hr":
            blocks.append("---")
            i += 1
            continue

        if ttype == "blockquote_open":
            depth = 1
            j = i + 1
            inner: list[Token] = []
            while j < n and depth > 0:
                if tokens[j].type == "blockquote_open":
                    depth += 1
                elif tokens[j].type == "blockquote_close":
                    depth -= 1
                    if depth == 0:
                        break
                inner.append(tokens[j])
                j += 1
            inner_md = _render_block_tokens(
                inner, protected_urls=protected_urls, roots=roots
            )
            quoted = "\n".join(
                f"> {line}" if line else ">" for line in inner_md.split("\n")
            )
            blocks.append(quoted)
            i = j + 1
            continue

        if ttype in ("bullet_list_open", "ordered_list_open"):
            ordered = ttype == "ordered_list_open"
            close_type = "ordered_list_close" if ordered else "bullet_list_close"
            start_num = int(tok.attrGet("start") or 1) if ordered else 1
            depth = 1
            j = i + 1
            inner = []
            while j < n and depth > 0:
                if tokens[j].type == ttype:
                    depth += 1
                elif tokens[j].type == close_type:
                    depth -= 1
                    if depth == 0:
                        break
                inner.append(tokens[j])
                j += 1
            list_lines = _render_list_items(
                inner,
                ordered=ordered,
                start_num=start_num,
                protected_urls=protected_urls,
                roots=roots,
            )
            if list_lines:
                blocks.append("\n".join(list_lines))
            i = j + 1
            continue

        if ttype == "table_open":
            depth = 1
            j = i + 1
            inner = []
            while j < n and depth > 0:
                if tokens[j].type == "table_open":
                    depth += 1
                elif tokens[j].type == "table_close":
                    depth -= 1
                    if depth == 0:
                        break
                inner.append(tokens[j])
                j += 1
            table_md = _render_table(
                inner, protected_urls=protected_urls, roots=roots
            )
            if table_md:
                blocks.append(table_md)
            i = j + 1
            continue

        i += 1

    return "\n\n".join(b for b in blocks if b)


def _render_list_items(
    tokens: list[Token],
    *,
    ordered: bool,
    start_num: int,
    protected_urls: tuple[str, ...],
    roots: tuple[str, ...],
) -> list[str]:
    lines: list[str] = []
    idx = start_num
    i = 0
    n = len(tokens)
    while i < n:
        if tokens[i].type == "list_item_open":
            depth = 1
            j = i + 1
            inner: list[Token] = []
            while j < n and depth > 0:
                if tokens[j].type == "list_item_open":
                    depth += 1
                elif tokens[j].type == "list_item_close":
                    depth -= 1
                    if depth == 0:
                        break
                inner.append(tokens[j])
                j += 1
            body = _render_block_tokens(
                inner, protected_urls=protected_urls, roots=roots
            )
            prefix = f"{idx}. " if ordered else "- "
            indent = " " * len(prefix)
            body_lines = body.split("\n") if body else [""]
            lines.append(f"{prefix}{body_lines[0]}")
            for bl in body_lines[1:]:
                lines.append(f"{indent}{bl}" if bl else "")
            idx += 1
            i = j + 1
            continue
        i += 1
    return lines


def _render_table(
    tokens: list[Token],
    *,
    protected_urls: tuple[str, ...],
    roots: tuple[str, ...],
) -> str:
    rows: list[list[str]] = []
    aligns: list[str] = []
    current_row: list[str] | None = None
    in_head = False

    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if t.type == "thead_open":
            in_head = True
        elif t.type == "thead_close":
            in_head = False
        elif t.type == "tr_open":
            current_row = []
        elif t.type == "tr_close":
            if current_row is not None:
                rows.append(current_row)
            current_row = None
        elif t.type in ("th_open", "td_open"):
            if in_head:
                style = str(t.attrGet("style") or "")
                if "right" in style:
                    aligns.append("---:")
                elif "center" in style:
                    aligns.append(":---:")
                elif "left" in style:
                    aligns.append(":---")
                else:
                    aligns.append("---")
            cell_text = ""
            if i + 1 < n and tokens[i + 1].type == "inline":
                cell_text = _render_inline_tokens(
                    tokens[i + 1].children,
                    protected_urls=protected_urls,
                    roots=roots,
                )
            cell_text = cell_text.replace("\n", " ").replace("|", "\\|")
            if current_row is not None:
                current_row.append(cell_text)
        i += 1

    if not rows:
        return ""
    col_count = max(len(r) for r in rows)
    if len(aligns) < col_count:
        aligns.extend(["---"] * (col_count - len(aligns)))

    def fmt_row(r: list[str]) -> str:
        padded = r + [""] * (col_count - len(r))
        return "| " + " | ".join(padded) + " |"

    out = [fmt_row(rows[0]), "| " + " | ".join(aligns[:col_count]) + " |"]
    for r in rows[1:]:
        out.append(fmt_row(r))
    return "\n".join(out)


def strip_embedded_context_blocks(text: str) -> str:
    """Remove ``<file-comments>...</file-comments>`` and ``<system-reminder>...</system-reminder>``."""
    if not text:
        return ""
    cleaned = _FILE_COMMENTS_RE.sub("", text)
    cleaned = _SYSTEM_REMINDER_RE.sub("", cleaned)
    return cleaned


def sanitize_shared_markdown(
    text: str,
    *,
    protected_urls: Iterable[str] = (),
    roots: Iterable[str] = (),
) -> str:
    """Full AST + HTML-whitelist sanitizer for ``allow_files=false`` markdown text."""
    if not text:
        return ""
    p_urls = tuple(str(u) for u in protected_urls if u)
    norm_roots = tuple(str(r).strip().rstrip("/") for r in roots if str(r or "").strip())

    cleaned = strip_embedded_context_blocks(text)
    if not cleaned.strip():
        return ""

    cleaned = _unwrap_backtick_file_refs(cleaned)
    tokens = _MD_PARSER.parse(cleaned)
    rebuilt = _render_block_tokens(tokens, protected_urls=p_urls, roots=norm_roots).strip()
    # Defense-in-depth: verify re-parsed AST introduces zero unauthorized resource/HTML nodes.
    if rebuilt and _has_disallowed_ast_resource_nodes(
        rebuilt, protected_urls=p_urls, roots=norm_roots
    ):
        rebuilt = _render_block_tokens(
            _MD_PARSER.parse(rebuilt), protected_urls=p_urls, roots=norm_roots
        ).strip()
        if rebuilt and _has_disallowed_ast_resource_nodes(
            rebuilt, protected_urls=p_urls, roots=norm_roots
        ):
            # Final fail-closed fallback: escape all remaining angle brackets/brackets
            rebuilt = _escape_markdown_text_chunk(
                scrub_protected_references(
                    rebuilt,
                    protected_urls=p_urls,
                    roots=norm_roots,
                    scrub_all_image_urls=True,
                )
            ).strip()
    return rebuilt


# =============================================================================
# SAFE TITLES, ORG LIBRARY & ACTIVITY LOG REDACTION
# =============================================================================

_HTML_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _to_plain_title_text(
    raw: str,
    *,
    protected_urls: Iterable[str] = (),
    roots: Iterable[str] = (),
) -> str:
    cleaned = sanitize_shared_markdown(raw, protected_urls=protected_urls, roots=roots)
    # Remove <redacted-image ...></redacted-image> and HTML tags
    plain = re.sub(r"<redacted-image\b[^>]*>.*?</redacted-image>", "", cleaned, flags=re.IGNORECASE)
    plain = _HTML_TAG_STRIP_RE.sub("", plain)
    # Strip markdown link syntax `[label](url)` -> `label`
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", plain)
    plain = re.sub(r"[#*_~`>]+", " ", plain)
    plain = scrub_protected_references(
        plain, protected_urls=protected_urls, roots=roots
    )
    plain = " ".join(plain.split()).strip()
    if plain in ("", "[redacted-url]", "[redacted-path]", "[已隐藏链接]"):
        return ""
    return plain


def resolve_safe_share_title(
    queries: Iterable[Mapping[str, Any]] | None,
    roots: Iterable[str] = (),
    protected_urls: Iterable[str] = (),
) -> str:
    """Derive public share metadata title strictly from current sanitized first ordinary query.

    Completely ignores ``conversation_threads.title`` so edited-turn legacy comment
    titles can never leak. Falls back to ``"Shared Conversation"``.
    """
    if not queries:
        return DEFAULT_SHARED_TITLE
    ordered = sorted(
        (q for q in queries if isinstance(q, Mapping)),
        key=lambda q: (
            q.get("turn_index") is None,
            q.get("turn_index") if isinstance(q.get("turn_index"), int) else 0,
        ),
    )
    if not ordered:
        return DEFAULT_SHARED_TITLE
    first = ordered[0]
    qtype = first.get("type")
    if qtype and (qtype in NOTIFICATION_QUERY_TYPES or qtype == "system"):
        return DEFAULT_SHARED_TITLE
    content = str(first.get("content") or "")
    plain = _to_plain_title_text(
        content, protected_urls=protected_urls, roots=roots
    )
    if not plain:
        return DEFAULT_SHARED_TITLE
    return plain[:60]


async def resolve_public_metadata_fields(
    thread: Mapping[str, Any], *, allow_files: bool
) -> dict[str, Any]:
    """Compute safe metadata fields for ``GET /api/v1/public/shared/{share_token}``.

    When ``allow_files=True``, preserves original fields.
    When ``allow_files=False``, blanks ``root_dir``/``dir_name``/``previous_root_dirs``,
    ignores ``conversation_threads.title`` (deriving title strictly from current
    sanitized first ordinary query or ``"Shared Conversation"``), and scrubs
    ``workspace_name``.
    """
    if allow_files:
        return {
            "title": thread.get("title"),
            "workspace_name": thread.get("workspace_name"),
            "root_dir": thread.get("root_dir"),
            "previous_root_dirs": list(thread.get("previous_root_dirs") or ()),
            "dir_name": thread.get("dir_name"),
        }

    roots = extract_thread_roots(thread)
    thread_id = str(thread.get("conversation_thread_id") or "")
    queries: list[dict[str, Any]] = []
    if thread_id:
        try:
            from src.server.database.pool import get_db_connection

            async with get_db_connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT turn_index, type, content, metadata
                        FROM conversation_queries
                        WHERE conversation_thread_id = %s::uuid
                        ORDER BY turn_index ASC
                        LIMIT 5
                        """,
                        (thread_id,),
                    )
                    queries = [dict(r) for r in await cur.fetchall()]
        except Exception:
            queries = []

    protected = collect_thread_protected_urls(queries=queries)
    safe_title = resolve_safe_share_title(queries, roots=roots, protected_urls=protected)
    raw_ws_name = thread.get("workspace_name")
    safe_ws_name = (
        scrub_protected_references(str(raw_ws_name), roots=roots, protected_urls=protected)
        if raw_ws_name is not None
        else None
    )
    return {
        "title": safe_title,
        "workspace_name": safe_ws_name,
        "root_dir": None,
        "previous_root_dirs": [],
        "dir_name": None,
    }


async def prepare_org_library_publish_fields(
    *,
    kind: str,
    thread: Mapping[str, Any] | None,
    existing_item_id: str | None = None,
    title: str | None,
    summary: str | None,
) -> tuple[str | None, str | None, bool]:
    """Sanitize publish inputs for thread items and determine ``is_explicit_custom_title``.

    Returns ``(effective_title, effective_summary, custom_title_flag)``.
    """
    if kind != "thread":
        return title, summary, bool(title)

    tid = str(thread.get("conversation_thread_id") or "") if isinstance(thread, Mapping) else ""
    if not existing_item_id and tid:
        existing_item_id = await _find_existing_org_library_thread_item_id(tid)

    eff = await resolve_effective_share_permissions(thread)
    roots = extract_thread_roots(thread)
    p_urls = (await _load_thread_protected_urls_map([tid])).get(tid, ()) if (tid and not eff.allow_files) else ()
    existing_custom = False
    if existing_item_id:
        try:
            meta = await get_org_library_title_meta_map([existing_item_id])
            existing_custom = bool(meta.get(str(existing_item_id), False))
        except Exception:
            existing_custom = False

    if title is not None and title.strip():
        clean_t = (
            clean_org_library_title(title, protected_urls=p_urls, roots=roots)
            if not eff.allow_files
            else title
        )
        clean_s = (
            clean_org_library_summary(summary, protected_urls=p_urls, roots=roots)
            if (summary is not None and not eff.allow_files)
            else summary
        )
        return clean_t, clean_s, True

    # Title omitted on publish/re-publish
    clean_s = (
        clean_org_library_summary(summary, protected_urls=p_urls, roots=roots)
        if (summary is not None and not eff.allow_files)
        else summary
    )
    if not existing_custom and not eff.allow_files:
        return DEFAULT_SHARED_TITLE, clean_s, False
    return title, clean_s, existing_custom


async def prepare_org_library_edit_fields(
    *,
    row: Mapping[str, Any],
    title: str | None,
    summary: str | None,
) -> tuple[str | None, str | None, bool, str]:
    """Sanitize edit inputs for thread items and compute ``(title, summary, custom_title, activity_title)``."""
    item_id = str(row["item_id"])
    if row.get("kind") != "thread":
        is_explicit = bool(title)
        act_title = (title or str(row.get("title") or ""))[:256]
        return title, summary, is_explicit, act_title

    tid = str(row.get("thread_id") or "")
    threads_map, protected_urls_map = await asyncio.gather(
        _load_threads_for_redaction([tid]) if tid else asyncio.sleep(0, result={}),
        _load_thread_protected_urls_map([tid]) if tid else asyncio.sleep(0, result={}),
    )
    trow = threads_map.get(tid)
    ws = {"user_id": trow.get("owner_user_id")} if trow and trow.get("owner_user_id") else None
    eff = await resolve_effective_share_permissions(trow, ws)
    roots = extract_thread_roots(trow)
    p_urls = protected_urls_map.get(tid, ())

    try:
        meta = await get_org_library_title_meta_map([item_id])
        existing_custom = bool(meta.get(item_id, False))
    except Exception:
        existing_custom = False

    clean_s = (
        clean_org_library_summary(summary, protected_urls=p_urls, roots=roots)
        if (summary is not None and not eff.allow_files)
        else summary
    )

    if title is not None and title.strip():
        clean_t = (
            clean_org_library_title(title, protected_urls=p_urls, roots=roots)
            if not eff.allow_files
            else title
        )
        return clean_t, clean_s, True, clean_t[:256]

    if not existing_custom and not eff.allow_files:
        return DEFAULT_SHARED_TITLE, clean_s, False, DEFAULT_SHARED_TITLE

    existing_title = str(row.get("title") or "")
    if not eff.allow_files:
        existing_title = clean_org_library_title(
            existing_title, protected_urls=p_urls, roots=roots
        )
    return title, clean_s, existing_custom, existing_title[:256]


async def resolve_org_library_remove_activity_title(
    removed: Mapping[str, Any], *, was_custom_title: bool
) -> tuple[str, bool]:
    """Compute ``(activity_title, custom_title_flag)`` for ``Action.LIBRARY_REMOVED``."""
    if removed.get("kind") != "thread":
        return str(removed.get("title") or "")[:256], was_custom_title
    tid = str(removed.get("thread_id") or "")
    threads_map = await _load_threads_for_redaction([tid]) if tid else {}
    trow = threads_map.get(tid)
    ws = {"user_id": trow.get("owner_user_id")} if trow and trow.get("owner_user_id") else None
    eff = await resolve_effective_share_permissions(trow, ws)
    if not eff.allow_files:
        if was_custom_title:
            roots = extract_thread_roots(trow)
            return clean_org_library_title(str(removed.get("title") or ""), roots=roots)[:256], True
        return DEFAULT_SHARED_TITLE, False
    return str(removed.get("title") or "")[:256], was_custom_title


def clean_org_library_summary(
    summary: str | None,
    *,
    protected_urls: Iterable[str] = (),
    roots: Iterable[str] = (),
) -> str:
    """Strip ``<file-comments>`` / ``<system-reminder>`` and scrub protected refs in org library summary."""
    if not summary:
        return ""
    cleaned = strip_embedded_context_blocks(summary)
    cleaned = scrub_protected_references(
        cleaned, protected_urls=protected_urls, roots=roots
    )
    return cleaned.strip()


def clean_org_library_title(
    title: str | None,
    *,
    protected_urls: Iterable[str] = (),
    roots: Iterable[str] = (),
) -> str:
    """Scrub explicit org library title; falls back to ``DEFAULT_SHARED_TITLE`` if empty."""
    if not title:
        return DEFAULT_SHARED_TITLE
    cleaned = strip_embedded_context_blocks(title)
    cleaned = scrub_protected_references(
        cleaned, protected_urls=protected_urls, roots=roots
    )
    cleaned = " ".join(cleaned.split()).strip()
    if not cleaned or cleaned in ("[redacted-url]", "[redacted-path]"):
        return DEFAULT_SHARED_TITLE
    return cleaned


async def upsert_org_library_title_meta(item_id: str, custom_title: bool) -> None:
    """Persist whether ``item_id`` has an explicit publisher-provided title."""
    from src.server.database.pool import get_db_connection

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO dalton_org_library_title_meta (item_id, custom_title, updated_at)
                    VALUES (%s::uuid, %s, NOW())
                    ON CONFLICT (item_id) DO UPDATE
                        SET custom_title = EXCLUDED.custom_title,
                            updated_at = NOW()
                    """,
                    (str(item_id), bool(custom_title)),
                )
    except RuntimeError as exc:
        if "pool is not open" in str(exc).lower():
            logger.debug("Skipping org library title meta upsert without open DB pool")
            return
        raise


async def get_org_library_title_meta_map(item_ids: Iterable[str]) -> dict[str, bool]:
    """Fetch ``{item_id: custom_title}`` for given library item IDs."""
    ids = [str(i) for i in item_ids if i]
    if not ids:
        return {}
    from src.server.database.pool import get_db_connection

    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT item_id::text AS item_id, custom_title
                    FROM dalton_org_library_title_meta
                    WHERE item_id = ANY(%s::uuid[])
                    """,
                    (ids,),
                )
                rows = await cur.fetchall()
        return {str(r["item_id"]): bool(r["custom_title"]) for r in rows}
    except RuntimeError as exc:
        if "pool is not open" in str(exc).lower():
            return {}
        raise


async def delete_org_library_title_meta(item_id: str) -> None:
    """Remove metadata row when a library item is deleted."""
    from src.server.database.pool import get_db_connection

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM dalton_org_library_title_meta WHERE item_id = %s::uuid",
                    (str(item_id),),
                )
    except RuntimeError as exc:
        if "pool is not open" in str(exc).lower():
            logger.debug("Skipping org library title meta delete without open DB pool")
            return
        raise


async def _find_existing_org_library_thread_item_id(thread_id: str) -> str | None:
    """Look up existing ``org_library_items.item_id`` for ``thread_id`` directly from DB."""
    if not thread_id:
        return None
    from src.server.database.pool import get_db_connection

    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT item_id::text AS item_id
                    FROM org_library_items
                    WHERE thread_id = %s::uuid
                    LIMIT 1
                    """,
                    (str(thread_id),),
                )
                row = await cur.fetchone()
        return str(row["item_id"]) if row and row.get("item_id") else None
    except RuntimeError as exc:
        if "pool is not open" in str(exc).lower():
            return None
        raise
    except Exception:
        return None


async def _load_threads_for_redaction(
    thread_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Batch-load thread rows + workspace roots (including ``w.previous_root_dirs``) for redaction."""
    ids = list({str(tid) for tid in thread_ids if tid})
    if not ids:
        return {}
    from src.server.database.pool import get_db_connection

    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT t.conversation_thread_id::text AS conversation_thread_id,
                           t.workspace_id::text AS workspace_id,
                           t.user_id,
                           t.is_shared,
                           t.share_permissions,
                           t.metadata->'fork' AS fork,
                           w.user_id AS owner_user_id,
                           w.status AS workspace_status,
                           w.config AS workspace_config,
                           w.previous_root_dirs AS previous_root_dirs,
                           c.root_dir AS computer_root_dir
                    FROM conversation_threads t
                    LEFT JOIN workspaces w ON w.workspace_id = t.workspace_id
                    LEFT JOIN computers c ON c.computer_id = w.computer_id
                    WHERE t.conversation_thread_id = ANY(%s::uuid[])
                    """,
                    (ids,),
                )
                rows = await cur.fetchall()
        return {str(r["conversation_thread_id"]): dict(r) for r in rows}
    except RuntimeError as exc:
        if "pool is not open" in str(exc).lower():
            return {}
        raise


async def _load_workspace_roots_map(
    workspace_ids: Iterable[str],
) -> dict[str, tuple[str, ...]]:
    """Batch-load current & historical root dirs for workspaces (for rows without ``thread_id``)."""
    ids = list({str(wid) for wid in workspace_ids if wid})
    if not ids:
        return {}
    from src.server.database.pool import get_db_connection

    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT w.workspace_id::text AS workspace_id,
                           w.config AS workspace_config,
                           w.previous_root_dirs AS previous_root_dirs,
                           c.root_dir AS computer_root_dir
                    FROM workspaces w
                    LEFT JOIN computers c ON c.computer_id = w.computer_id
                    WHERE w.workspace_id = ANY(%s::uuid[])
                    """,
                    (ids,),
                )
                rows = await cur.fetchall()
        return {str(r["workspace_id"]): extract_thread_roots(r) for r in rows}
    except RuntimeError as exc:
        if "pool is not open" in str(exc).lower():
            return {}
        raise
    except Exception:
        return {}


async def enforce_shared_file_permissions(
    thread: Mapping[str, Any],
    workspace: Mapping[str, Any] | None = None,
    *,
    require_files: bool = False,
    require_download: bool = False,
) -> None:
    """Enforce effective ``allow_files`` / ``allow_download`` on shared file routes (thin-hook target for ``share_access.py``)."""
    if not (require_files or require_download):
        return
    from fastapi import HTTPException

    deny_headers = {"Cache-Control": "no-store", "Clear-Site-Data": '"cache"'}
    eff = await resolve_effective_share_permissions(thread, workspace)
    if not eff.allow_files:
        detail = (
            "仅管理员开启的公开对话分享允许访问工作区文件"
            if workspace is not None
            else "Permission 'allow_files' not granted for this shared thread"
        )
        raise HTTPException(status_code=403, detail=detail, headers=deny_headers)
    if require_download and not eff.allow_download:
        raise HTTPException(
            status_code=403,
            detail="Permission 'allow_download' not granted for this shared thread",
            headers=deny_headers,
        )


async def prepare_public_thread_metadata(
    thread: Mapping[str, Any],
    response: Any,
) -> dict[str, Any]:
    """Compute effective permissions, cache-clearing header, and safe metadata fields for ``public.py``."""
    eff = await resolve_effective_share_permissions(thread)
    if not eff.allow_files and response is not None:
        response.headers["Clear-Site-Data"] = '"cache"'
    fields = await resolve_public_metadata_fields(thread, allow_files=eff.allow_files)
    return {
        **fields,
        "allow_files": eff.allow_files,
        "allow_download": eff.allow_download,
    }


async def _load_thread_protected_urls_map(
    thread_ids: Iterable[str],
) -> dict[str, set[str]] | None:
    """Batch-load each thread's registered ``protected_urls`` across ALL attempts.

    Returns ``{}`` only when the DB pool is not open (unit tests without DB).
    Returns ``None`` on any other DB error so callers fail closed (safe title only,
    no summary or snippets).
    """
    ids = list({str(tid) for tid in thread_ids if tid})
    if not ids:
        return {}
    from src.server.database.pool import get_db_connection

    queries_by_tid: dict[str, list[dict[str, Any]]] = {}
    responses_by_tid: dict[str, list[dict[str, Any]]] = {}
    run_facts_by_tid: dict[str, list[dict[str, Any]]] = {}
    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT conversation_thread_id::text AS thread_id, metadata
                    FROM conversation_queries
                    WHERE conversation_thread_id = ANY(%s::uuid[])
                      AND metadata IS NOT NULL
                    """,
                    (ids,),
                )
                for row in await cur.fetchall():
                    tid = str(row["thread_id"])
                    queries_by_tid.setdefault(tid, []).append(dict(row))

                await cur.execute(
                    """
                    SELECT conversation_thread_id::text AS thread_id, turn_index, replay_facts
                    FROM conversation_responses
                    WHERE conversation_thread_id = ANY(%s::uuid[])
                      AND replay_facts IS NOT NULL
                    """,
                    (ids,),
                )
                for row in await cur.fetchall():
                    tid = str(row["thread_id"])
                    responses_by_tid.setdefault(tid, []).append(dict(row))

                await cur.execute(
                    """
                    SELECT thread_id::text AS thread_id, replay_facts
                    FROM subagent_runs
                    WHERE thread_id = ANY(%s::uuid[])
                      AND replay_facts IS NOT NULL
                    """,
                    (ids,),
                )
                for row in await cur.fetchall():
                    tid = str(row["thread_id"])
                    run_facts_by_tid.setdefault(tid, []).append(dict(row))
    except RuntimeError as exc:
        if "pool is not open" in str(exc).lower():
            return {}
        logger.warning(
            "Failed loading protected_urls for org-library/activity redaction; failing closed",
            exc_info=True,
        )
        return None
    except Exception:
        logger.warning(
            "Failed loading protected_urls for org-library/activity redaction; failing closed",
            exc_info=True,
        )
        return None

    out: dict[str, set[str]] = {}
    for tid in ids:
        urls = collect_thread_protected_urls(
            queries_by_tid.get(tid),
            responses_by_tid.get(tid),
            run_facts_by_tid.get(tid),
        )
        if urls:
            out[tid] = urls
    return out


def extract_thread_roots(thread: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Extract filesystem roots to scrub for a thread/workspace."""
    if not isinstance(thread, Mapping):
        return ()
    roots: list[str] = []
    for key in ("root_dir", "computer_root_dir"):
        val = str(thread.get(key) or "").strip()
        if val and val not in roots:
            roots.append(val)
    prev = thread.get("previous_root_dirs")
    if isinstance(prev, (list, tuple)):
        for p in prev:
            s = str(p or "").strip()
            if s and s not in roots:
                roots.append(s)
    ws_cfg = thread.get("workspace_config")
    if isinstance(ws_cfg, dict):
        for p in ws_cfg.get("previous_root_dirs") or ():
            s = str(p or "").strip()
            if s and s not in roots:
                roots.append(s)
    return tuple(roots)


async def redact_org_library_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Redact org library rows' titles and summaries when linked thread has effective ``allow_files=False``."""
    if not rows:
        return []

    thread_rows_input = [r for r in rows if r.get("kind") == "thread"]

    item_ids = [str(r["item_id"]) for r in thread_rows_input if r.get("item_id")]
    thread_ids = [str(r["thread_id"]) for r in thread_rows_input if r.get("thread_id")]

    if thread_ids or item_ids:
        meta_map, threads_map, protected_urls_map = await asyncio.gather(
            get_org_library_title_meta_map(item_ids),
            _load_threads_for_redaction(thread_ids),
            _load_thread_protected_urls_map(thread_ids),
        )
    else:
        meta_map, threads_map, protected_urls_map = {}, {}, {}

    protected_urls_failed = protected_urls_map is None
    safe_protected_map = protected_urls_map if protected_urls_map is not None else {}

    eff_cache: dict[str, EffectiveSharePermissions] = {}
    for tid in thread_ids:
        trow = threads_map.get(tid)
        if trow is None:
            eff_cache[tid] = EffectiveSharePermissions(False, False)
        else:
            ws = {"user_id": trow.get("owner_user_id")} if trow.get("owner_user_id") else None
            eff_cache[tid] = await resolve_effective_share_permissions(trow, ws)

    result: list[dict[str, Any]] = []
    for row in rows:
        copied = dict(row)
        kind = copied.get("kind")
        if kind != "thread":
            if kind == "skill":
                copied["title"] = clean_org_library_title(copied.get("title"))
            copied["summary"] = clean_org_library_summary(copied.get("summary"))
            result.append(copied)
            continue

        tid = str(copied.get("thread_id") or "")
        trow = threads_map.get(tid) if tid else None
        eff = eff_cache.get(tid, EffectiveSharePermissions(False, False))
        if not eff.allow_files:
            if protected_urls_failed:
                # Fail closed: emit only the generic safe title, omitting summary/snippets.
                copied["title"] = DEFAULT_SHARED_TITLE
                copied["summary"] = ""
            else:
                roots = extract_thread_roots(trow)
                p_urls = safe_protected_map.get(tid, ())
                is_custom = meta_map.get(str(copied.get("item_id") or ""), False)
                if is_custom:
                    copied["title"] = clean_org_library_title(
                        copied.get("title"), protected_urls=p_urls, roots=roots
                    )
                else:
                    copied["title"] = DEFAULT_SHARED_TITLE
                copied["summary"] = clean_org_library_summary(
                    copied.get("summary"), protected_urls=p_urls, roots=roots
                )
        result.append(copied)
    return result


def _scrub_detail_value(
    val: Any,
    *,
    protected_urls: Iterable[str] = (),
    roots: tuple[str, ...] = (),
) -> Any:
    if isinstance(val, str):
        return scrub_protected_references(
            val, protected_urls=protected_urls, roots=roots
        )
    if isinstance(val, list):
        return [
            _scrub_detail_value(v, protected_urls=protected_urls, roots=roots)
            for v in val
        ]
    if isinstance(val, tuple):
        return [
            _scrub_detail_value(v, protected_urls=protected_urls, roots=roots)
            for v in val
        ]
    if isinstance(val, dict):
        return {
            k: _scrub_detail_value(v, protected_urls=protected_urls, roots=roots)
            for k, v in val.items()
        }
    return val


async def redact_activity_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Redact all activity log rows on ``GET /api/v1/activity``.

    - Matches ``Action.LIBRARY_PUBLISHED``, ``Action.LIBRARY_UPDATED``,
      ``Action.LIBRARY_REMOVED`` (and any row carrying ``thread_id`` + ``detail.title``).
    - Judges each row's title strictly by that row's own ``detail.get("custom_title") is True``.
    - Strips internal ``custom_title`` marker from output ``detail``.
    - Redacts ``detail.path`` for both thread-bound (``allow_files=False``) and
      workspace-only rows (e.g. ``FILE_PUBLISHED`` / ``FILE_UNSHARED`` without ``thread_id``).
    - Scrubs protected references (including thread ``protected_urls``), historical roots,
      and relative workspace paths across ALL rows and ALL ``Action`` types.
    """
    if not rows:
        return []

    from src.server.services.activity import Action

    library_actions = frozenset(
        {
            Action.LIBRARY_PUBLISHED.value,
            Action.LIBRARY_UPDATED.value,
            Action.LIBRARY_REMOVED.value,
            Action.LIBRARY_PUBLISHED,
            Action.LIBRARY_UPDATED,
            Action.LIBRARY_REMOVED,
        }
    )

    thread_ids = [str(r["thread_id"]) for r in rows if r.get("thread_id")]
    workspace_ids_no_thread = [
        str(r["workspace_id"])
        for r in rows
        if not r.get("thread_id") and r.get("workspace_id")
    ]
    threads_map, protected_urls_map, ws_roots_map = await asyncio.gather(
        _load_threads_for_redaction(thread_ids),
        _load_thread_protected_urls_map(thread_ids),
        _load_workspace_roots_map(workspace_ids_no_thread),
    )

    protected_urls_failed = protected_urls_map is None
    safe_protected_map = protected_urls_map if protected_urls_map is not None else {}

    eff_cache: dict[str, EffectiveSharePermissions] = {}
    for tid in set(thread_ids):
        trow = threads_map.get(tid)
        if trow is None:
            eff_cache[tid] = EffectiveSharePermissions(False, False)
        else:
            ws = {"user_id": trow.get("owner_user_id")} if trow.get("owner_user_id") else None
            eff_cache[tid] = await resolve_effective_share_permissions(trow, ws)

    out: list[dict[str, Any]] = []
    for row in rows:
        copied = dict(row)
        raw_detail = copied.get("detail")
        if isinstance(raw_detail, str):
            try:
                raw_detail = json.loads(raw_detail)
            except Exception:
                raw_detail = {}
        detail = dict(raw_detail) if isinstance(raw_detail, Mapping) else {}

        tid = str(copied.get("thread_id") or "")
        wid = str(copied.get("workspace_id") or "")
        trow = threads_map.get(tid) if tid else None
        roots = extract_thread_roots(trow) if trow is not None else ws_roots_map.get(wid, ())
        p_urls = safe_protected_map.get(tid, ())
        eff = (
            eff_cache.get(tid, EffectiveSharePermissions(False, False))
            if tid
            else EffectiveSharePermissions(False, False)
        )

        action_val = copied.get("action")
        is_library_thread = (
            action_val in library_actions and detail.get("kind") == "thread"
        )
        has_thread_title = bool(tid) and "title" in detail
        is_custom_title = detail.get("custom_title") is True

        # Always remove internal marker before returning
        detail.pop("custom_title", None)

        if protected_urls_failed and tid and not eff.allow_files:
            # Fail closed: keep only safe title (and structural kind when present), drop summary/snippets.
            safe_detail: dict[str, Any] = {}
            if is_library_thread:
                safe_detail["kind"] = "thread"
            if is_library_thread or has_thread_title:
                safe_detail["title"] = DEFAULT_SHARED_TITLE
            copied["detail"] = safe_detail
            out.append(copied)
            continue

        if (is_library_thread or has_thread_title) and not eff.allow_files:
            if is_custom_title:
                detail["title"] = clean_org_library_title(
                    str(detail.get("title") or ""),
                    protected_urls=p_urls,
                    roots=roots,
                )
            else:
                detail["title"] = DEFAULT_SHARED_TITLE

        # Redact `detail["path"]` when allow_files=False (both thread-bound and
        # workspace-only rows such as Action.FILE_PUBLISHED which carry no thread_id).
        if not eff.allow_files and isinstance(detail.get("path"), str) and detail["path"].strip():
            detail["path"] = "[redacted-path]"

        # Scrub all string values & string lists across every activity row
        for k, v in list(detail.items()):
            if k == "path" and v == "[redacted-path]":
                continue
            detail[k] = _scrub_detail_value(v, protected_urls=p_urls, roots=roots)

        copied["detail"] = detail
        out.append(copied)
    return out


# =============================================================================
# NESTED NOTIFICATION METADATA RECONSTRUCTION (9-KEY WHITELIST)
# =============================================================================

def _rebuild_wakeup_notice(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    rebuilt: dict[str, Any] = {}
    for key in SAFE_WAKEUP_NOTICE_FIELDS:
        if key in item and item[key] is not None:
            rebuilt[key] = item[key]
    return rebuilt if rebuilt else None


def _rebuild_market_target_notice(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    rebuilt: dict[str, Any] = {}
    for key in SAFE_MARKET_TARGET_FIELDS:
        if key in item and item[key] is not None:
            rebuilt[key] = item[key]
    return rebuilt if rebuilt else None


def rebuild_user_message_metadata(raw_meta: Any) -> dict[str, Any]:
    """Rebuild ``user_message.metadata`` from empty dict with strict 9-key notification whitelist."""
    if not isinstance(raw_meta, Mapping):
        return {}
    safe: dict[str, Any] = {}
    msg_type = raw_meta.get("msg_type")
    if isinstance(msg_type, str) and msg_type:
        safe["msg_type"] = msg_type

    raw_wakeup = raw_meta.get("wakeup")
    if isinstance(raw_wakeup, Mapping):
        raw_notices = raw_wakeup.get("notices")
        if isinstance(raw_notices, list):
            rebuilt_notices = [
                n for item in raw_notices if (n := _rebuild_wakeup_notice(item)) is not None
            ]
            safe["wakeup"] = {"v": 1, "notices": rebuilt_notices}
        else:
            single = _rebuild_wakeup_notice(raw_wakeup)
            if single is not None:
                safe["wakeup"] = {"v": 1, "notices": [single]}

    raw_market = raw_meta.get("market_target")
    if isinstance(raw_market, Mapping):
        raw_notices = raw_market.get("notices")
        if isinstance(raw_notices, list):
            rebuilt_targets = [
                n
                for item in raw_notices
                if (n := _rebuild_market_target_notice(item)) is not None
            ]
            safe["market_target"] = {"notices": rebuilt_targets}
        else:
            single = _rebuild_market_target_notice(raw_market)
            if single is not None:
                safe["market_target"] = {"notices": [single]}

    return safe


# =============================================================================
# TOP-LEVEL JSON SCALAR SCANNER FOR LARGE TOOL FRAMES (NO FULL JSON.LOADS)
# =============================================================================

def _skip_json_string(s: str, i: int) -> int:
    """Given ``s[i] == '"'``, return index immediately after closing quote."""
    n = len(s)
    i += 1
    while i < n:
        ch = s[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return i + 1
        i += 1
    return n


def _skip_json_ws(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] in " \t\r\n":
        i += 1
    return i


def _skip_json_value(s: str, i: int) -> int:
    """Skip one JSON value starting at ``i`` without allocating substrings."""
    n = len(s)
    i = _skip_json_ws(s, i)
    if i >= n:
        return n
    ch = s[i]
    if ch == '"':
        return _skip_json_string(s, i)
    if ch in "{[":
        depth = 0
        while i < n:
            c = s[i]
            if c == '"':
                i = _skip_json_string(s, i)
                continue
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return n
    # Primitive (number, true, false, null)
    while i < n and s[i] not in ",}] \t\r\n":
        i += 1
    return i


def _count_json_array_elements(s: str, i: int) -> int:
    """Count top-level elements of JSON array starting at ``s[i] == '['``."""
    n = len(s)
    if i >= n or s[i] != "[":
        return 0
    i = _skip_json_ws(s, i + 1)
    if i < n and s[i] == "]":
        return 0
    count = 0
    while i < n:
        i = _skip_json_ws(s, i)
        if i >= n or s[i] == "]":
            break
        count += 1
        i = _skip_json_value(s, i)
        i = _skip_json_ws(s, i)
        if i < n and s[i] == ",":
            i += 1
        elif i < n and s[i] == "]":
            break
    return count


def extract_tool_line_meta(
    data_json: str, *, include_call_count: bool = False
) -> tuple[str | None, Any, int]:
    """Scan JSON object at depth 1 to extract ``(agent, turn_index, tool_calls_count)``.

    Avoids ``json.loads`` on multi-megabyte ``tool_calls`` / ``tool_call_result``
    frames so giant ``content`` / ``args`` strings are never unescaped or allocated.
    """
    n = len(data_json)
    i = _skip_json_ws(data_json, 0)
    if i >= n or data_json[i] != "{":
        return None, None, 0
    i += 1

    agent: str | None = None
    turn_index: Any = None
    call_count = 0
    found_agent = False
    found_turn = False
    found_calls = not include_call_count

    while i < n:
        i = _skip_json_ws(data_json, i)
        if i >= n or data_json[i] == "}":
            break
        if data_json[i] != '"':
            break
        key_end = _skip_json_string(data_json, i)
        raw_key = data_json[i + 1 : key_end - 1]
        i = _skip_json_ws(data_json, key_end)
        if i >= n or data_json[i] != ":":
            break
        i = _skip_json_ws(data_json, i + 1)

        if raw_key == "agent":
            val_end = _skip_json_value(data_json, i)
            token_str = data_json[i:val_end].strip()
            if token_str == "null":
                agent = None
            elif token_str.startswith('"'):
                try:
                    agent = json.loads(token_str)
                except Exception:
                    agent = token_str.strip('"')
            found_agent = True
            i = val_end
        elif raw_key == "turn_index":
            val_end = _skip_json_value(data_json, i)
            token_str = data_json[i:val_end].strip()
            try:
                turn_index = json.loads(token_str)
            except Exception:
                turn_index = None
            found_turn = True
            i = val_end
        elif include_call_count and raw_key == "tool_calls":
            if i < n and data_json[i] == "[":
                call_count = _count_json_array_elements(data_json, i)
            i = _skip_json_value(data_json, i)
            found_calls = True
        else:
            i = _skip_json_value(data_json, i)

        if found_agent and found_turn and found_calls:
            break

        i = _skip_json_ws(data_json, i)
        if i < n and data_json[i] == ",":
            i += 1
        elif i < n and data_json[i] == "}":
            break

    return agent, turn_index, call_count


# =============================================================================
# SINGLEFLIGHT COLD PROJECTION & BACKGROUND SLICE WARMING
# =============================================================================

async def _warm_shared_thread_slices_bg(
    thread_id: str,
    *,
    before_turn: Any | None = None,
    limit: int | None = PUBLIC_SHARE_MAX_TURNS,
    budget: Any | None = None,
) -> bool:
    """Acquire cross-process Redis lock and warm raw unredacted ``turn_slices``."""
    from src.server.database.conversation.replay_rows import get_replay_thread_data
    from src.server.services.history.replay import lines_epoch, read_replay_page
    from src.utils.cache.redis_cache import get_cache_client

    epoch = lines_epoch()
    lock_key = f"share_slice_warm:{thread_id}:{epoch}"
    try:
        cache = get_cache_client()
        acquired = await cache.acquire_lock(lock_key, "1", 60_000)
        if acquired is not True:
            return False
    except Exception:
        # If Redis is unavailable, skip background warming gracefully
        logger.debug("Redis lock unavailable for share_slice_warm; skipping background warm")
        return False

    try:
        rows = await get_replay_thread_data(thread_id)
        if rows is None:
            return False
        effective_budget = budget if budget is not None else get_public_share_page_budget()
        await read_replay_page(
            rows,
            before_turn=before_turn,
            limit=limit,
            budget=effective_budget,
            persist=True,
        )
        return True
    except Exception:
        logger.warning(
            "Background share slice warm failed for thread %s",
            thread_id,
            exc_info=True,
        )
        return False


def schedule_warm_shared_thread_slices(
    thread_id: str,
    *,
    before_turn: Any | None = None,
    limit: int | None = PUBLIC_SHARE_MAX_TURNS,
    budget: Any | None = None,
) -> asyncio.Task[bool] | None:
    """Fire-and-forget background task to warm raw ``turn_slices`` for a shared thread."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(
        _warm_shared_thread_slices_bg(
            thread_id, before_turn=before_turn, limit=limit, budget=budget
        ),
        name=f"share-warm-{thread_id[:8]}",
    )
    _BG_WARM_TASKS.add(task)
    task.add_done_callback(_BG_WARM_TASKS.discard)
    return task


async def read_strict_public_replay_page(
    rows: Any,
    *,
    before_turn: Any | None = None,
    limit: int | None = PUBLIC_SHARE_MAX_TURNS,
    budget: Any | None = None,
    trigger_background_warm: bool = True,
) -> tuple[Any, str]:
    """Read a strict ``allow_files=False`` replay page with process-local Singleflight.

    - Main request path always passes ``persist=False`` (strictly read-only).
    - Concurrent requests for the same ``(thread_id, before_turn, lines_epoch())``
      coalesce onto a single shared ``Task`` awaited via ``asyncio.shield()``, so
      cancelling any waiter never cancels the leader or raises ``InvalidStateError``.
    - Schedules non-blocking Redis-locked background ``persist=True`` warming ONLY
      when an actual cache miss occurred (``cold.project_stale`` ran), never on warm hits.
    """
    from src.server.services.history.replay import lines_epoch, read_replay_page

    effective_budget = budget if budget is not None else get_public_share_page_budget()
    key = (str(rows.thread_id), before_turn, lines_epoch())

    existing = _COLD_SINGLEFLIGHT.get(key)
    if existing is not None:
        return await asyncio.shield(existing)

    async def _leader() -> tuple[Any, str]:
        miss_flag = [False]
        token = _COLD_PROJECTION_HIT.set(miss_flag)
        try:
            result = await read_replay_page(
                rows,
                before_turn=before_turn,
                limit=limit,
                budget=effective_budget,
                persist=False,
            )
            if trigger_background_warm and miss_flag[0] and result[1] == "checkpoint":
                schedule_warm_shared_thread_slices(
                    str(rows.thread_id),
                    before_turn=before_turn,
                    limit=limit,
                    budget=effective_budget,
                )
            return result
        finally:
            _COLD_PROJECTION_HIT.reset(token)

    loop = asyncio.get_running_loop()
    task: asyncio.Task[tuple[Any, str]] = loop.create_task(_leader())
    _COLD_SINGLEFLIGHT[key] = task

    def _cleanup(done_task: asyncio.Task[Any]) -> None:
        if _COLD_SINGLEFLIGHT.get(key) is done_task:
            _COLD_SINGLEFLIGHT.pop(key, None)
        # Retrieve exception if all callers disconnected to avoid unhandled task warnings
        if not done_task.cancelled():
            done_task.exception()

    task.add_done_callback(_cleanup)
    return await asyncio.shield(task)


# =============================================================================
# STRICT NO-FILES SHARE REDACTOR (ALLOW_FILES=FALSE)
# =============================================================================

_ALLOWED_STRICT_EVENTS = frozenset(
    {
        "user_message",
        "steering_delivered",
        "message_chunk",
        "artifact",
        "error",
        "tool_calls",
        "tool_call_result",
    }
)


def _is_main_channel(agent: Any) -> bool:
    return agent in _MAIN_AGENTS


def collect_thread_protected_urls(
    queries: Iterable[Mapping[str, Any]] | None = None,
    responses_by_turn: Mapping[Any, Any] | Iterable[Mapping[str, Any]] | None = None,
    run_facts: Iterable[Mapping[str, Any]] | None = None,
) -> set[str]:
    """Gather all registered media/attachment/preview URLs for a thread across all attempts."""
    urls: set[str] = set()
    for q in queries or ():
        meta = q.get("metadata") if isinstance(q, Mapping) else None
        if isinstance(meta, Mapping):
            for att in meta.get("attachments") or ():
                if isinstance(att, Mapping):
                    if att.get("url"):
                        urls.add(str(att["url"]))
                    if att.get("object_key"):
                        urls.add(str(att["object_key"]))
    resp_rows: list[Mapping[str, Any]] = []
    if isinstance(responses_by_turn, Mapping):
        for val in responses_by_turn.values():
            if isinstance(val, Mapping):
                resp_rows.append(val)
            elif isinstance(val, (list, tuple)):
                for item in val:
                    if isinstance(item, Mapping):
                        resp_rows.append(item)
    elif responses_by_turn is not None:
        for item in responses_by_turn:
            if isinstance(item, Mapping):
                resp_rows.append(item)
    for resp in resp_rows:
        facts = resp.get("replay_facts")
        if isinstance(facts, Mapping):
            imgs = facts.get("images")
            if isinstance(imgs, Mapping):
                for u in imgs.values():
                    if u:
                        urls.add(str(u))
            elif isinstance(imgs, list):
                for u in imgs:
                    if u:
                        urls.add(str(u))
    for rf in run_facts or ():
        if not isinstance(rf, Mapping):
            continue
        facts = rf.get("replay_facts") if "replay_facts" in rf else rf
        if isinstance(facts, Mapping):
            imgs = facts.get("images")
            if isinstance(imgs, Mapping):
                for u in imgs.values():
                    if u:
                        urls.add(str(u))
            elif isinstance(imgs, list):
                for u in imgs:
                    if u:
                        urls.add(str(u))
    return urls


class StrictNoFilesShareRedactor:
    """Streaming, empty-dict whitelist redactor for ``allow_files=False`` replay."""

    def __init__(
        self,
        *,
        thread_id: str,
        responses_by_turn: Mapping[Any, Mapping[str, Any]] | None = None,
        open_runs: Mapping[Any, Any] | None = None,
        roots: Iterable[str] = (),
        protected_urls: Iterable[str] = (),
    ) -> None:
        self._thread_id = str(thread_id)
        self._responses_by_turn = dict(responses_by_turn or {})
        self._open_runs = dict(open_runs or {})
        self._roots = tuple(str(r).strip().rstrip("/") for r in roots if str(r or "").strip())
        self._protected_urls: set[str] = {str(u) for u in protected_urls if u}

    def iter_redacted_events(
        self, lines: Iterable[Line | Mapping[str, Any]]
    ) -> Iterator[dict[str, Any]]:
        """Yield redacted ``{"event": ..., "data": ...}`` frames turn-by-turn."""
        current_turn: Any = _UNSET
        turn_entries: list[tuple[str, Any]] = []
        last_seen_turn: Any = None

        for raw in lines:
            parsed = self._parse_line(raw, fallback_turn=last_seen_turn)
            if parsed is None:
                continue
            kind, turn_idx, payload = parsed
            if turn_idx is not None:
                last_seen_turn = turn_idx
            if current_turn is _UNSET:
                current_turn = turn_idx
            elif turn_idx != current_turn:
                yield from self._flush_turn(current_turn, turn_entries)
                turn_entries.clear()
                current_turn = turn_idx
            turn_entries.append((kind, payload))

        if current_turn is not _UNSET and turn_entries:
            yield from self._flush_turn(current_turn, turn_entries)
            turn_entries.clear()

    def _parse_line(
        self, raw: Line | Mapping[str, Any], *, fallback_turn: Any
    ) -> tuple[str, Any, Any] | None:
        if isinstance(raw, Line):
            ev = raw.event
            if ev not in _ALLOWED_STRICT_EVENTS:
                return None
            if ev in ("tool_calls", "tool_call_result"):
                agent, ti, _ = extract_tool_line_meta(raw.data_json)
                if not _is_main_channel(agent):
                    return None
                return ("tool_boundary", ti if ti is not None else fallback_turn, None)
            if ev == "message_chunk":
                if _REASONING_CONTENT_TYPE_RE.search(raw.data_json):
                    return None
            if ev == "artifact":
                if '"html_widget"' not in raw.data_json and '"preview_url"' not in raw.data_json:
                    return None
            try:
                data = json.loads(raw.data_json)
            except Exception:
                return None
        else:
            ev = str(raw.get("event") or "")
            if ev not in _ALLOWED_STRICT_EVENTS:
                return None
            data = raw.get("data")
            if not isinstance(data, Mapping):
                return None
            if ev in ("tool_calls", "tool_call_result"):
                if not _is_main_channel(data.get("agent")):
                    return None
                ti = data.get("turn_index")
                return ("tool_boundary", ti if ti is not None else fallback_turn, None)

        if not isinstance(data, Mapping):
            return None

        # Pre-dispatch main-channel gate (blocks task:*, subagent:*, agent, etc.)
        if not _is_main_channel(data.get("agent")):
            return None

        ti = data.get("turn_index")
        if ti is None:
            ti = fallback_turn

        if ev == "error":
            return ("main_error", ti, None)

        if ev == "artifact":
            atype = data.get("artifact_type")
            if atype == "preview_url":
                payload = data.get("payload")
                if isinstance(payload, Mapping) and payload.get("url"):
                    self._protected_urls.add(str(payload["url"]))
                return None
            if atype != "html_widget":
                return None
            return ("html_widget", ti, dict(data))

        if ev == "message_chunk":
            role = data.get("role")
            if role not in (None, "assistant"):
                return None
            ctype = data.get("content_type")
            if ctype not in ("text", None):
                return None
            return ("message_chunk", ti, dict(data))

        if ev == "user_message":
            return ("user_message", ti, dict(data))

        if ev == "steering_delivered":
            return ("steering_delivered", ti, dict(data))

        return None

    def _flush_turn(
        self, turn_index: Any, entries: list[tuple[str, Any]]
    ) -> Iterator[dict[str, Any]]:
        has_main_error = any(k == "main_error" for k, _ in entries)
        has_stopped = any(
            k == "message_chunk" and isinstance(p, Mapping) and p.get("finish_reason") == "stopped"
            for k, p in entries
        )

        # Collect any extra protected URLs from user_message attachments in this turn
        for k, p in entries:
            if k == "user_message" and isinstance(p, Mapping):
                meta = p.get("metadata")
                if isinstance(meta, Mapping):
                    for att in meta.get("attachments") or ():
                        if isinstance(att, Mapping) and att.get("url"):
                            self._protected_urls.add(str(att["url"]))

        # Determine turn eligibility
        resp = self._responses_by_turn.get(turn_index)
        resp_status = str(resp.get("status") or "").lower() if isinstance(resp, Mapping) else None
        is_open = turn_index in self._open_runs

        is_failed = (resp_status == "error") or has_main_error
        is_cancelled = (not is_failed) and ((resp_status == "cancelled") or has_stopped)
        is_in_progress = (not is_failed) and (not is_cancelled) and (
            is_open or resp is None or resp_status in ("in_progress", "active", "interrupted")
        )
        is_completed = (
            not is_failed
            and not is_cancelled
            and not is_in_progress
            and resp_status == "completed"
        )

        # Check if this turn is a notification-only turn
        turn_is_notification = False
        for k, p in entries:
            if k == "user_message" and isinstance(p, Mapping):
                qtype = p.get("query_type")
                if qtype in NOTIFICATION_QUERY_TYPES or qtype == "system":
                    turn_is_notification = True

        # Split entries into cycles separated by user_message / steering_delivered
        cycles: list[list[tuple[str, Any]]] = []
        current_cycle: list[tuple[str, Any]] = []
        for entry in entries:
            kind, payload = entry
            if kind in ("user_message", "steering_delivered"):
                if current_cycle:
                    cycles.append(current_cycle)
                    current_cycle = []
                current_cycle.append(entry)
            else:
                current_cycle.append(entry)
        if current_cycle:
            cycles.append(current_cycle)

        emitted_assistant_text = False
        emitted_widget = False
        emitted_any_user_or_steer = False

        # For non-completed turns, emit sanitized user/steering messages first, then one fixed status notice
        if not is_completed:
            for kind, payload in entries:
                if kind == "user_message":
                    rebuilt = self._rebuild_user_message(payload, turn_index)
                    if rebuilt is not None:
                        emitted_any_user_or_steer = True
                        yield {"event": "user_message", "data": rebuilt}
                elif kind == "steering_delivered":
                    rebuilt = self._rebuild_steering(payload, turn_index)
                    if rebuilt is not None:
                        emitted_any_user_or_steer = True
                        yield {"event": "steering_delivered", "data": rebuilt}

            if is_failed:
                notice = FAILED_TURN_NOTICE
            elif is_cancelled:
                notice = CANCELLED_TURN_NOTICE
            else:
                notice = IN_PROGRESS_TURN_NOTICE
            yield {
                "event": "message_chunk",
                "data": self._build_assistant_chunk(
                    turn_index=turn_index,
                    chunk_id=f"redacted-status-{turn_index}",
                    content=notice,
                ),
            }
            return

        # Completed turn: process cycle-by-cycle
        for cycle_idx, cycle in enumerate(cycles):
            # Emit leading user_message or steering_delivered
            body_start = 0
            if cycle and cycle[0][0] == "user_message":
                rebuilt = self._rebuild_user_message(cycle[0][1], turn_index)
                if rebuilt is not None:
                    emitted_any_user_or_steer = True
                    yield {"event": "user_message", "data": rebuilt}
                body_start = 1
            elif cycle and cycle[0][0] == "steering_delivered":
                rebuilt = self._rebuild_steering(cycle[0][1], turn_index)
                if rebuilt is not None:
                    emitted_any_user_or_steer = True
                    yield {"event": "steering_delivered", "data": rebuilt}
                body_start = 1

            cycle_body = cycle[body_start:]
            selected_chunks, widgets = self._select_cycle_final_output(cycle_body)

            merged_text = "".join(str(c.get("content") or "") for c in selected_chunks)
            merged_text = strip_inline_thinking_markers(merged_text)
            sanitized_text = sanitize_shared_markdown(
                merged_text,
                protected_urls=self._protected_urls,
                roots=self._roots,
            )
            if sanitized_text:
                chunk_id = str(
                    (selected_chunks[-1].get("id") if selected_chunks else None)
                    or f"redacted-{turn_index}-{cycle_idx}"
                )
                yield {
                    "event": "message_chunk",
                    "data": self._build_assistant_chunk(
                        turn_index=turn_index,
                        chunk_id=chunk_id,
                        content=sanitized_text,
                    ),
                }
                emitted_assistant_text = True

            for w in widgets:
                artifact_id = str(w.get("artifact_id") or f"widget-{turn_index}-{cycle_idx}")
                yield {
                    "event": "artifact",
                    "data": {
                        "thread_id": self._thread_id,
                        "turn_index": turn_index,
                        "agent": "main",
                        "artifact_type": "html_widget",
                        "artifact_id": artifact_id,
                        "status": "completed",
                        "payload": {"redacted": True},
                    },
                }
                emitted_widget = True

        # Pure-tool / empty completed non-notification turn fallback
        if (
            not emitted_assistant_text
            and not emitted_widget
            and not turn_is_notification
            and emitted_any_user_or_steer
        ):
            yield {
                "event": "message_chunk",
                "data": self._build_assistant_chunk(
                    turn_index=turn_index,
                    chunk_id=f"redacted-tools-only-{turn_index}",
                    content=TOOLS_ONLY_TURN_NOTICE,
                ),
            }

    def _apply_message_opening_thinking_scan(
        self, cycle_body: list[tuple[str, Any]]
    ) -> list[tuple[str, Any]]:
        """Scan all text events per message id across all phases using ``scan_all_thinking_spans``.

        Runs BEFORE phase selection so thinking blocks that follow normal prefix
        text, span ``commentary`` -> ``final_answer``, or contain literal closing
        tags inside Markdown/blockquote code fences never leak residual body text
        into ``final_answer``:
        - Unclosed opening thinking block at message start -> drops ALL text chunks
          of that message.
        - Closed thinking spans anywhere in the message and any unclosed thinking
          tail after prefix text -> mapped back onto each chunk across all phases
          before phase selection.
        """
        from src.dalton.thinking_spans import scan_all_thinking_spans
        from src.server.utils.thinking_leak import decide_opening

        # Group indices of text message_chunk entries by message id (preserving order).
        groups: dict[str, list[int]] = {}
        for idx, (k, p) in enumerate(cycle_body):
            if k != "message_chunk" or not isinstance(p, dict):
                continue
            raw_id = str(p.get("id") or "").strip()
            group_key = raw_id if raw_id else f"__anon_{idx}"
            groups.setdefault(group_key, []).append(idx)

        replacements: dict[int, str | None] = {}
        for indices in groups.values():
            parts = [str(cycle_body[i][1].get("content") or "") for i in indices]
            full_text = "".join(parts)
            if not full_text:
                continue
            scan = scan_all_thinking_spans(full_text)
            if (
                scan.unclosed_start is not None
                and not scan.strip_spans
                and full_text[: scan.unclosed_start].strip() == ""
            ):
                for i in indices:
                    replacements[i] = None
                continue
            spans_to_strip: list[tuple[int, int]] = list(scan.strip_spans)
            for _s_start, s_end in list(scan.strip_spans):
                if s_end < len(full_text):
                    after_opening = decide_opening(full_text[s_end:])
                    if after_opening is not None and after_opening.kind == "need":
                        spans_to_strip.append((s_end, s_end + after_opening.end))
            if scan.unclosed_start is not None:
                spans_to_strip.append((scan.unclosed_start, len(full_text)))
            if not spans_to_strip:
                continue
            spans_to_strip.sort()
            offset = 0
            for i, text_part in zip(indices, parts):
                b_start = offset
                b_end = offset + len(text_part)
                offset = b_end
                cursor = b_start
                kept: list[str] = []
                for s_start, s_end in spans_to_strip:
                    if s_end <= cursor:
                        continue
                    if s_start >= b_end:
                        break
                    if s_start > cursor:
                        kept.append(text_part[cursor - b_start : s_start - b_start])
                    cursor = max(cursor, min(s_end, b_end))
                if cursor < b_end:
                    kept.append(text_part[cursor - b_start : b_end - b_start])
                remaining = "".join(kept)
                replacements[i] = remaining if remaining else None

        if not replacements:
            return cycle_body

        filtered: list[tuple[str, Any]] = []
        for idx, (k, p) in enumerate(cycle_body):
            if idx not in replacements:
                filtered.append((k, p))
                continue
            new_text = replacements[idx]
            if new_text is None:
                continue
            filtered.append((k, {**p, "content": new_text}))
        return filtered

    def _select_cycle_final_output(
        self, cycle_body: list[tuple[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Select final text chunks (Case A phase-aware or Case B trailing post-tool) and widgets."""
        cycle_body = self._apply_message_opening_thinking_scan(cycle_body)
        widgets = [p for k, p in cycle_body if k == "html_widget" and isinstance(p, dict)]

        last_tool_pos = -1
        for idx, (k, _) in enumerate(cycle_body):
            if k == "tool_boundary":
                last_tool_pos = idx

        text_positions: list[tuple[int, dict[str, Any]]] = [
            (idx, p)
            for idx, (k, p) in enumerate(cycle_body)
            if k == "message_chunk" and isinstance(p, dict) and str(p.get("content") or "")
        ]
        if not text_positions:
            return [], widgets

        has_explicit_phase = any(
            p.get("phase") in ("commentary", "final_answer") for _, p in text_positions
        )

        if has_explicit_phase:
            # Case A: only phase == "final_answer" after last tool call
            selected = [
                p
                for idx, p in text_positions
                if idx > last_tool_pos and p.get("phase") == "final_answer"
            ]
            return selected, widgets

        # Case B: no explicit phase -> trailing contiguous main text chunks after last tool call
        selected = [
            p
            for idx, p in text_positions
            if idx > last_tool_pos and p.get("finish_reason") in ("stop", None, "")
        ]
        return selected, widgets

    def _build_assistant_chunk(
        self, *, turn_index: Any, chunk_id: str, content: str
    ) -> dict[str, Any]:
        return {
            "thread_id": self._thread_id,
            "turn_index": turn_index,
            "agent": "main",
            "id": chunk_id,
            "role": "assistant",
            "content": content,
            "content_type": "text",
            "finish_reason": "stop",
            "phase": "final_answer",
        }

    def _rebuild_user_message(
        self, data: Mapping[str, Any], turn_index: Any
    ) -> dict[str, Any] | None:
        qtype = data.get("query_type")
        if qtype == "system":
            return None

        raw_meta = data.get("metadata")
        safe_meta = rebuild_user_message_metadata(raw_meta)

        rebuilt: dict[str, Any] = {
            "thread_id": self._thread_id,
            "turn_index": turn_index,
            "timestamp": data.get("timestamp"),
            "metadata": safe_meta,
        }
        if data.get("run_completed_at") is not None:
            rebuilt["run_completed_at"] = data["run_completed_at"]

        if qtype == "wakeup":
            rebuilt["query_type"] = "wakeup"
            rebuilt["content"] = WAKEUP_FIXED_NOTICE
            return rebuilt

        if qtype == "market_target":
            rebuilt["query_type"] = "market_target"
            rebuilt["content"] = MARKET_ALERT_FIXED_NOTICE
            return rebuilt

        raw_content = str(data.get("content") or "")
        sanitized = sanitize_shared_markdown(
            raw_content,
            protected_urls=self._protected_urls,
            roots=self._roots,
        )
        if not sanitized:
            att_count = 0
            if isinstance(raw_meta, Mapping) and isinstance(raw_meta.get("attachments"), list):
                att_count = len(raw_meta["attachments"])
            if att_count > 0:
                sanitized = f"[已附带 {att_count} 个文件（当前分享仅展示文字）]"
        rebuilt["content"] = sanitized
        return rebuilt

    def _rebuild_steering(
        self, data: Mapping[str, Any], turn_index: Any
    ) -> dict[str, Any] | None:
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list):
            return None

        safe_messages: list[dict[str, Any]] = []
        for msg in raw_messages:
            if not isinstance(msg, Mapping):
                continue
            if msg.get("report_source") is not None:
                continue
            ts = msg.get("timestamp")
            if "wakeup" in msg and isinstance(msg.get("wakeup"), Mapping):
                safe_notice = _rebuild_wakeup_notice(msg["wakeup"])
                entry: dict[str, Any] = {
                    "content": WAKEUP_FIXED_NOTICE,
                    "timestamp": ts,
                }
                if safe_notice is not None:
                    entry["wakeup"] = safe_notice
                safe_messages.append(entry)
                continue
            if "market_alert" in msg and isinstance(msg.get("market_alert"), Mapping):
                safe_alert = _rebuild_market_target_notice(msg["market_alert"])
                entry = {
                    "content": MARKET_ALERT_FIXED_NOTICE,
                    "timestamp": ts,
                }
                if safe_alert is not None:
                    entry["market_alert"] = safe_alert
                safe_messages.append(entry)
                continue

            sanitized = sanitize_shared_markdown(
                str(msg.get("content") or ""),
                protected_urls=self._protected_urls,
                roots=self._roots,
            )
            if sanitized:
                safe_messages.append({"content": sanitized, "timestamp": ts})

        if not safe_messages:
            return None

        return {
            "thread_id": self._thread_id,
            "turn_index": turn_index,
            "agent": "main",
            "timestamp": data.get("timestamp"),
            "count": len(safe_messages),
            "messages": safe_messages,
        }


_UNSET = object()


# =============================================================================
# DB-ONLY SHARE PANEL REDACTION SUMMARY (0 CHECKPOINT / 0 STORAGE I/O)
# =============================================================================

def _count_images_in_text(text: str) -> int:
    if not text:
        return 0
    return len(_MD_IMAGE_COUNT_RE.findall(text))


def _summarize_turn_lines_text(
    lines_text: str, *, status: str | None
) -> tuple[int, int, int]:
    """Return ``(widgets, tool_calls, images)`` from a stored ``turn_slices.lines`` blob."""
    widgets = 0
    tool_calls = 0
    images = 0

    cycle_entries: list[tuple[str, Any]] = []
    for raw in lines_text.split("\n"):
        if not raw:
            continue
        parts = raw.split("\t", 2)
        if len(parts) != 3:
            continue
        event, _flags, data_json = parts
        if event == "tool_calls":
            agent, _ti, count = extract_tool_line_meta(data_json, include_call_count=True)
            if _is_main_channel(agent):
                tool_calls += max(count, 1)
                cycle_entries.append(("tool_boundary", None))
        elif event == "tool_call_result":
            agent, _ti, _ = extract_tool_line_meta(data_json)
            if _is_main_channel(agent):
                cycle_entries.append(("tool_boundary", None))
        elif event == "artifact":
            if '"html_widget"' not in data_json:
                continue
            try:
                data = json.loads(data_json)
            except Exception:
                continue
            if (
                isinstance(data, Mapping)
                and _is_main_channel(data.get("agent"))
                and data.get("artifact_type") == "html_widget"
            ):
                widgets += 1
        elif event == "message_chunk" and status == "completed":
            if _REASONING_CONTENT_TYPE_RE.search(data_json):
                continue
            try:
                data = json.loads(data_json)
            except Exception:
                continue
            if (
                isinstance(data, Mapping)
                and _is_main_channel(data.get("agent"))
                and data.get("role") in (None, "assistant")
                and data.get("content_type") in ("text", None)
            ):
                cycle_entries.append(("message_chunk", dict(data)))
        elif event in ("user_message", "steering_delivered") and status == "completed":
            cycle_entries.append(("boundary", None))

    if status == "completed" and cycle_entries:
        # Count images only in the final text segments of each cycle
        redactor = StrictNoFilesShareRedactor(thread_id="")
        current: list[tuple[str, Any]] = []
        cycles: list[list[tuple[str, Any]]] = []
        for entry in cycle_entries:
            if entry[0] == "boundary":
                if current:
                    cycles.append(current)
                    current = []
            else:
                current.append(entry)
        if current:
            cycles.append(current)
        for cyc in cycles:
            selected, _ = redactor._select_cycle_final_output(cyc)
            merged = strip_inline_thinking_markers(
                "".join(str(c.get("content") or "") for c in selected)
            )
            images += _count_images_in_text(merged)

    return widgets, tool_calls, images


def _summarize_replay_facts(
    replay_facts: Any, *, status: str | None
) -> tuple[int, int, int]:
    """Fallback summary from ``conversation_responses.replay_facts`` when ``turn_slices`` is absent."""
    if not isinstance(replay_facts, Mapping):
        return 0, 0, 0
    widgets = 0
    tool_calls = 0
    images = 0

    imgs = replay_facts.get("images")
    if isinstance(imgs, Mapping):
        images += len(imgs)
    elif isinstance(imgs, list):
        images += len(imgs)

    legacy = replay_facts.get("legacy")
    if isinstance(legacy, Mapping):
        leg_widgets = legacy.get("widgets")
        if isinstance(leg_widgets, (list, dict)):
            widgets += len(leg_widgets)
        verbatim = legacy.get("verbatim")
        if isinstance(verbatim, list):
            for entry in verbatim:
                if not isinstance(entry, Mapping):
                    continue
                ev = entry.get("event")
                data = entry.get("data")
                if not isinstance(data, Mapping) or not _is_main_channel(data.get("agent")):
                    continue
                if ev == "tool_calls":
                    calls = data.get("tool_calls")
                    tool_calls += len(calls) if isinstance(calls, list) and calls else 1
                elif ev == "artifact" and data.get("artifact_type") == "html_widget":
                    widgets += 1
                elif (
                    ev == "message_chunk"
                    and status == "completed"
                    and data.get("content_type") in ("text", None)
                ):
                    images += _count_images_in_text(str(data.get("content") or ""))

    return widgets, tool_calls, images


async def summarize_thread_share_redaction(thread_id: str) -> dict[str, int]:
    """Compute share-panel redaction summary strictly from persisted DB rows.

    Makes ZERO calls to ``finish_lines``, object storage, ``stamp_replay_task_status``,
    or ``CheckpointHistoryReader``.
    """
    from src.server.database.pool import get_db_connection

    widgets = 0
    tool_calls = 0
    images = 0
    attachments = 0

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # 1. Queries
            await cur.execute(
                """
                SELECT turn_index, type, content, metadata
                FROM conversation_queries
                WHERE conversation_thread_id = %s::uuid
                ORDER BY turn_index ASC
                """,
                (str(thread_id),),
            )
            query_rows = await cur.fetchall()

            # 2. Settled responses + stored turn_slices.lines
            await cur.execute(
                """
                SELECT DISTINCT ON (r.turn_index)
                       r.turn_index,
                       r.status,
                       r.replay_facts,
                       ts.lines
                FROM conversation_responses r
                LEFT JOIN turn_slices ts
                  ON ts.conversation_response_id = r.conversation_response_id
                WHERE r.conversation_thread_id = %s::uuid
                  AND r.status <> 'in_progress'
                ORDER BY r.turn_index ASC, r.attempt_no DESC
                """,
                (str(thread_id),),
            )
            resp_rows = await cur.fetchall()

    for q in query_rows:
        qtype = q.get("type")
        if qtype in NOTIFICATION_QUERY_TYPES or qtype == "system":
            continue
        meta = q.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        meta = meta if isinstance(meta, Mapping) else {}
        atts = meta.get("attachments")
        if isinstance(atts, list):
            attachments += len(atts)
        art_comments = meta.get("artifact_comments")
        if isinstance(art_comments, Mapping) and isinstance(art_comments.get("comments"), list):
            attachments += len(art_comments["comments"])
        elif "<file-comments" in str(q.get("content") or "").lower():
            attachments += 1
        images += _count_images_in_text(
            strip_embedded_context_blocks(str(q.get("content") or ""))
        )

    for r in resp_rows:
        status = str(r.get("status") or "").lower()
        lines_blob = r.get("lines")
        if isinstance(lines_blob, str) and lines_blob:
            w, tc, img = _summarize_turn_lines_text(lines_blob, status=status)
        else:
            w, tc, img = _summarize_replay_facts(r.get("replay_facts"), status=status)
        widgets += w
        tool_calls += tc
        images += img

    return {
        "widgets": widgets,
        "tool_calls": tool_calls,
        "images": images,
        "attachments": attachments,
    }


# =============================================================================
# THIN-HOOK ORCHESTRATORS FOR UPSTREAM ROUTERS & SERVICES
# =============================================================================


async def post_update_thread_share_hook(
    thread_id: str, *, is_shared: bool
) -> dict[str, int] | None:
    """Background-warm slices on share enable and compute DB-only redaction summary."""
    if is_shared:
        schedule_warm_shared_thread_slices(thread_id)
    try:
        return await summarize_thread_share_redaction(thread_id)
    except Exception:
        return None


async def _resolve_borrowed_source_permissions(
    src_trow: Mapping[str, Any] | None,
) -> EffectiveSharePermissions:
    """Resolve effective share permissions for a borrowed ancestor thread (fail-closed)."""
    if not isinstance(src_trow, Mapping):
        return EffectiveSharePermissions(allow_files=False, allow_download=False)
    if src_trow.get("is_shared") is False:
        return EffectiveSharePermissions(allow_files=False, allow_download=False)
    ws = (
        {"user_id": src_trow.get("owner_user_id")}
        if src_trow.get("owner_user_id")
        else None
    )
    return await resolve_effective_share_permissions(src_trow, ws)


def _prepare_public_fork_divider(
    divider_data: Mapping[str, Any],
    *,
    thread_id: str,
    dropped_sources: Mapping[str, str],
) -> dict[str, Any]:
    """Anonymize a ``fork_divider`` frame for public replay and attach neutral unavailable notice."""
    from src.server.services.share_redaction import (
        _OWNER_ID_KEYS,
        _anonymous_divider,
        without_keys,
    )

    adjusted: dict[str, Any] = dict(divider_data)
    if dropped_sources:
        raw_segments = adjusted.get("segments")
        if isinstance(raw_segments, list):
            kept_segments = [
                seg
                for seg in raw_segments
                if not (
                    isinstance(seg, Mapping)
                    and str(seg.get("thread_id") or "") in dropped_sources
                )
            ]
            adjusted["segments"] = kept_segments
            adjusted["shown_through"] = (
                kept_segments[-1].get("through_turn")
                if kept_segments and isinstance(kept_segments[-1], Mapping)
                else None
            )
        src_id = str(adjusted.get("source_thread_id") or "")
        if src_id in dropped_sources:
            adjusted["state"] = dropped_sources[src_id]
        elif adjusted.get("state") == "shown":
            adjusted["state"] = "changed"

    anonymized = _anonymous_divider(without_keys(adjusted, _OWNER_ID_KEYS))
    anonymized["thread_id"] = thread_id
    if anonymized.get("state") in ("deleted", "unavailable"):
        anonymized["notice"] = FORK_SOURCE_UNAVAILABLE_NOTICE
    return anonymized


def _redact_rich_part_items(
    items: list[dict[str, Any]],
    *,
    thread_id: str,
    shared_user_message_fn: Any,
    share_redaction_cls: Any,
) -> list[dict[str, Any]]:
    """Run ``ShareRedaction`` on one thread part and rewrite ``thread_id`` to the shared fork's id."""
    redaction = share_redaction_cls(items)
    out: list[dict[str, Any]] = []
    for item in items:
        event_type, data = item["event"], item["data"]
        if event_type == "user_message":
            replay_data = shared_user_message_fn(redaction, data)
        else:
            replay_data = redaction.event(event_type, data)
            if replay_data is None:
                continue
        replay_data["thread_id"] = thread_id
        out.append({"event": event_type, "data": replay_data})
    return out


async def _build_borrowed_fork_public_replay(
    *,
    share_token: str,
    thread: Mapping[str, Any],
    thread_id: str,
    rows: Any,
    lineage: Any,
    eff: EffectiveSharePermissions,
    before_turn: int | None,
    budget: Any,
    observe_replay_stream_fn: Any,
    shared_user_message_fn: Any,
    share_redaction_cls: Any,
) -> Any:
    """Public replay orchestrator for a snapshot fork that borrows earlier turns from ancestors."""
    from fastapi.responses import StreamingResponse

    from src.dalton.share_media import (
        resolve_thread_media_bindings,
        rewrite_items_for_public_media,
    )
    from src.server.services.history.replay import finish_lines
    from src.server.services.history.replay.borrowed import read_thread_page

    viewer = lineage.by if getattr(lineage, "by", None) else rows.owner_id
    thread_page = await read_thread_page(
        rows,
        viewer,
        before_turn=before_turn,
        limit=PUBLIC_SHARE_MAX_TURNS,
        budget=budget,
        persist=False,
    )

    borrowed_tids = [
        str(part.thread_id)
        for part in thread_page.parts
        if str(part.thread_id) != thread_id and part.lines
    ]
    source_threads_map: dict[str, dict[str, Any]] = {}
    source_meta_failed = False
    if borrowed_tids:
        try:
            source_threads_map = await _load_threads_for_redaction(borrowed_tids)
        except Exception:
            logger.warning(
                "Failed loading borrowed ancestor threads for public share %s; failing closed",
                thread_id,
                exc_info=True,
            )
            source_meta_failed = True

    emitted_events: list[dict[str, Any]] = []
    dropped_sources: dict[str, str] = {}

    for part in thread_page.parts:
        if not part.lines:
            continue
        part_tid = str(part.thread_id)
        is_borrowed = part_tid != thread_id
        part_rows = part.rows if getattr(part, "rows", None) is not None else (rows if not is_borrowed else None)

        if is_borrowed:
            if source_meta_failed:
                dropped_sources[part_tid] = "unavailable"
                continue
            src_trow = source_threads_map.get(part_tid)
            if src_trow is None or src_trow.get("workspace_status") == "deleted":
                dropped_sources[part_tid] = "deleted"
                continue
            src_eff = await _resolve_borrowed_source_permissions(src_trow)
            part_allow_files = eff.allow_files and src_eff.allow_files
            part_thread_meta: Mapping[str, Any] = src_trow
        else:
            part_allow_files = eff.allow_files
            part_thread_meta = thread

        if not part_allow_files:
            strict = StrictNoFilesShareRedactor(
                thread_id=thread_id,
                responses_by_turn=(
                    part_rows.responses_by_turn if part_rows is not None else {}
                ),
                open_runs=part_rows.open_runs if part_rows is not None else {},
                roots=extract_thread_roots(part_thread_meta),
                protected_urls=(
                    collect_thread_protected_urls(
                        part_rows.queries,
                        part_rows.responses_by_turn,
                        part_rows.run_facts,
                    )
                    if part_rows is not None
                    else ()
                ),
            )
            emitted_events.extend(strict.iter_redacted_events(part.lines))
            continue

        # Rich mode (part_allow_files=True): stamp task status/widgets against the owning thread,
        # resolve & rewrite media bindings against that thread, then redact per-source.
        await finish_lines(part_tid, part.lines, status_only=True)
        part_items = [line.item() for line in part.lines]
        thread_for_media = dict(part_thread_meta)
        thread_for_media.setdefault("conversation_thread_id", part_tid)
        part_owner = (
            getattr(part_rows, "owner_id", None)
            or getattr(part, "owner_id", None)
            or part_thread_meta.get("owner_user_id")
        )
        if part_owner and not thread_for_media.get("user_id"):
            thread_for_media["user_id"] = str(part_owner)
        if not thread_for_media.get("workspace_id") and part_rows is not None:
            ws_from_rows = (part_rows.thread or {}).get("workspace_id")
            if ws_from_rows:
                thread_for_media["workspace_id"] = str(ws_from_rows)

        from src.dalton.share_media import match_borrowed_segment_index

        seg_index = match_borrowed_segment_index(lineage, part) if is_borrowed else None
        if is_borrowed and seg_index is None:
            bindings = None
        else:
            bindings = await resolve_thread_media_bindings(
                thread_for_media,
                part_rows.queries if part_rows is not None else (),
                part_items,
            )
        part_items = rewrite_items_for_public_media(
            part_items,
            thread_id=thread_id,
            share_token=share_token,
            bindings=bindings,
            source_segment_index=seg_index,
            source_thread_id=part_tid if is_borrowed else None,
        )
        emitted_events.extend(
            _redact_rich_part_items(
                part_items,
                thread_id=thread_id,
                shared_user_message_fn=shared_user_message_fn,
                share_redaction_cls=share_redaction_cls,
            )
        )

    divider_payload: dict[str, Any] | None = None
    if thread_page.divider is not None:
        raw_div = thread_page.divider.item().get("data")
        if isinstance(raw_div, Mapping):
            divider_payload = _prepare_public_fork_divider(
                raw_div,
                thread_id=thread_id,
                dropped_sources=dropped_sources,
            )
    elif dropped_sources and not emitted_events:
        # Paginating entirely into a deleted/unavailable ancestor range still yields a neutral placeholder.
        last_src = lineage.borrow.ancestors[-1].thread_id
        state = dropped_sources.get(str(last_src), next(iter(dropped_sources.values())))
        divider_payload = _prepare_public_fork_divider(
            {
                "source_thread_id": str(last_src),
                "turn_base": lineage.borrow.turn_base,
                "state": state,
                "shown_through": None,
                "segments": [],
            },
            thread_id=thread_id,
            dropped_sources=dropped_sources,
        )

    first_turn_index = thread_page.first_turn_index
    if dropped_sources:
        emitted_turns = [
            ev["data"]["turn_index"]
            for ev in emitted_events
            if isinstance(ev.get("data"), Mapping)
            and isinstance(ev["data"].get("turn_index"), int)
            and not isinstance(ev["data"].get("turn_index"), bool)
        ]
        first_turn_index = min(emitted_turns) if emitted_turns else None

    emit_history_page = bool(thread_page.has_more or before_turn is not None)
    headers = (
        {"Cache-Control": "no-store", "Clear-Site-Data": '"cache"'}
        if not eff.allow_files
        else {"Cache-Control": "no-store"}
    )

    async def borrowed_event_generator():
        seq = 0
        if emit_history_page:
            seq += 1
            hp = {
                "first_turn_index": first_turn_index,
                "has_more": thread_page.has_more,
            }
            yield (
                f"id: {seq}\n"
                f"event: history_page\n"
                f"data: {json.dumps(hp, default=str)}\n\n"
            )
        if divider_payload is not None:
            seq += 1
            yield (
                f"id: {seq}\n"
                f"event: fork_divider\n"
                f"data: {json.dumps(divider_payload, ensure_ascii=False, default=str)}\n\n"
            )
        for item in emitted_events:
            seq += 1
            yield (
                f"id: {seq}\n"
                f"event: {item['event']}\n"
                f"data: {json.dumps(item['data'], ensure_ascii=False, default=str)}\n\n"
            )
        seq += 1
        yield f"id: {seq}\nevent: replay_done\ndata: {json.dumps({'thread_id': thread_id}, default=str)}\n\n"

    return StreamingResponse(
        observe_replay_stream_fn(borrowed_event_generator(), source="public"),
        media_type="text/event-stream",
        headers=headers,
    )


async def build_public_replay_response(
    share_token: str,
    *,
    before_turn: int | None = None,
    get_shared_thread_fn: Any = None,
    get_replay_thread_data_fn: Any = None,
    observe_replay_stream_fn: Any = None,
    shared_user_message_fn: Any = None,
    share_redaction_cls: Any = None,
) -> Any:
    """Full AU1 public replay SSE orchestrator (thin-hook target for ``public.py``)."""
    from fastapi import HTTPException
    from fastapi.responses import StreamingResponse

    from src.dalton.share_media import (
        resolve_thread_media_bindings,
        rewrite_items_for_public_media,
    )
    from src.server.app import public as public_app
    from src.server.database.pool import DATABASE_UNAVAILABLE
    from src.server.services.forks.model import ForkLineage
    from src.server.services.history.replay import finish_lines, read_replay_page
    from src.server.utils.api import database_unavailable

    get_shared_thread_fn = get_shared_thread_fn or public_app.get_shared_thread
    get_replay_thread_data_fn = (
        get_replay_thread_data_fn or public_app.get_replay_thread_data
    )
    observe_replay_stream_fn = (
        observe_replay_stream_fn or public_app.observe_replay_stream
    )
    shared_user_message_fn = (
        shared_user_message_fn or public_app._shared_user_message
    )
    share_redaction_cls = share_redaction_cls or public_app.ShareRedaction

    no_store_headers = {"Cache-Control": "no-store"}
    try:
        try:
            thread = await get_shared_thread_fn(share_token)
        except HTTPException as exc:
            hdrs = dict(exc.headers or {})
            hdrs.setdefault("Cache-Control", "no-store")
            raise HTTPException(
                status_code=exc.status_code, detail=exc.detail, headers=hdrs
            ) from None
        thread_id = str(thread["conversation_thread_id"])
        eff = await resolve_effective_share_permissions(thread)
        rows = await get_replay_thread_data_fn(thread_id)
        if rows is None:
            raise HTTPException(
                status_code=404,
                detail="Shared thread not found",
                headers=no_store_headers,
            )
        budget = get_public_share_page_budget()
        rows_thread = getattr(rows, "thread", None)
        lineage = ForkLineage.parse(
            rows_thread.get("fork") if isinstance(rows_thread, Mapping) else None
        )
        if lineage is not None and lineage.borrow is not None and lineage.borrow.ancestors:
            return await _build_borrowed_fork_public_replay(
                share_token=share_token,
                thread=thread,
                thread_id=thread_id,
                rows=rows,
                lineage=lineage,
                eff=eff,
                before_turn=before_turn,
                budget=budget,
                observe_replay_stream_fn=observe_replay_stream_fn,
                shared_user_message_fn=shared_user_message_fn,
                share_redaction_cls=share_redaction_cls,
            )
        if not eff.allow_files:
            page, _ = await read_strict_public_replay_page(
                rows,
                before_turn=before_turn,
                limit=PUBLIC_SHARE_MAX_TURNS,
                budget=budget,
            )
        else:
            page, _ = await read_replay_page(
                rows,
                before_turn=before_turn,
                limit=PUBLIC_SHARE_MAX_TURNS,
                budget=budget,
                persist=True,
            )
            await finish_lines(thread_id, page.lines, status_only=True)
    except DATABASE_UNAVAILABLE as e:
        exc = database_unavailable("replaying a shared thread", logger, e)
        hdrs = dict(getattr(exc, "headers", None) or {})
        hdrs.setdefault("Cache-Control", "no-store")
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers=hdrs
        ) from None

    emit_history_page = bool(page.has_more or before_turn is not None)

    if not eff.allow_files:
        strict = StrictNoFilesShareRedactor(
            thread_id=thread_id,
            responses_by_turn=rows.responses_by_turn,
            open_runs=rows.open_runs,
            roots=extract_thread_roots(thread),
            protected_urls=collect_thread_protected_urls(
                rows.queries, rows.responses_by_turn, rows.run_facts
            ),
        )

        async def strict_event_generator():
            seq = 0
            if emit_history_page:
                seq += 1
                hp = {
                    "first_turn_index": page.first_turn_index,
                    "has_more": page.has_more,
                }
                yield (
                    f"id: {seq}\n"
                    f"event: history_page\n"
                    f"data: {json.dumps(hp, default=str)}\n\n"
                )
            for item in strict.iter_redacted_events(page.lines):
                seq += 1
                yield (
                    f"id: {seq}\n"
                    f"event: {item['event']}\n"
                    f"data: {json.dumps(item['data'], ensure_ascii=False, default=str)}\n\n"
                )
            seq += 1
            yield f"id: {seq}\nevent: replay_done\ndata: {json.dumps({'thread_id': thread_id}, default=str)}\n\n"

        return StreamingResponse(
            observe_replay_stream_fn(strict_event_generator(), source="public"),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "Clear-Site-Data": '"cache"',
            },
        )

    items = [line.item() for line in page.lines]
    thread_for_media = dict(thread)
    rows_owner = getattr(rows, "owner_id", None)
    if rows_owner and not thread_for_media.get("user_id"):
        thread_for_media["user_id"] = str(rows_owner)
    bindings = await resolve_thread_media_bindings(
        thread_for_media,
        rows.queries,
        items,
    )
    items = rewrite_items_for_public_media(
        items, thread_id=thread_id, share_token=share_token, bindings=bindings
    )
    redaction = share_redaction_cls(items)

    async def event_generator():
        seq = 0
        if emit_history_page:
            seq += 1
            hp = {
                "first_turn_index": page.first_turn_index,
                "has_more": page.has_more,
            }
            yield (
                f"id: {seq}\n"
                f"event: history_page\n"
                f"data: {json.dumps(hp, default=str)}\n\n"
            )
        for item in items:
            event_type, data = item["event"], item["data"]
            if event_type == "user_message":
                replay_data = shared_user_message_fn(redaction, data)
            else:
                replay_data = redaction.event(event_type, data)
                if replay_data is None:
                    continue
                replay_data.setdefault("thread_id", thread_id)
            seq += 1
            yield (
                f"id: {seq}\n"
                f"event: {event_type}\n"
                f"data: {json.dumps(replay_data, ensure_ascii=False, default=str)}\n\n"
            )

        seq += 1
        yield f"id: {seq}\nevent: replay_done\ndata: {json.dumps({'thread_id': thread_id}, default=str)}\n\n"

    return StreamingResponse(
        observe_replay_stream_fn(event_generator(), source="public"),
        media_type="text/event-stream",
        headers=no_store_headers,
    )


async def resolve_org_library_remove_meta(
    item_id: str, removed: Mapping[str, Any]
) -> dict[str, Any]:
    """Compute activity detail & delete per-item custom_title metadata on org-library removal."""
    kind = str(removed.get("kind") or "")
    detail: dict[str, Any] = {"kind": kind, "title": str(removed.get("title") or "")[:256]}
    if kind != "thread":
        return detail
    meta_map = await get_org_library_title_meta_map([item_id])
    was_custom = bool(meta_map.get(str(item_id), False))
    act_title, custom_flag = await resolve_org_library_remove_activity_title(
        removed, was_custom_title=was_custom
    )
    await delete_org_library_title_meta(item_id)
    detail["title"] = act_title
    detail["custom_title"] = bool(custom_flag)
    return detail
