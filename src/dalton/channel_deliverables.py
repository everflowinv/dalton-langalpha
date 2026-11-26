"""Channel Phase 2 Batch 1: DM turn deliverables recording, dual-path validation, and verified download.

Implements:
- §4.3 dual-path (requested relative path + resolved realpath) whitelist, excluded dirs,
  excluded extensions, magic-byte validation, and 20 MB quota check.
- Lead Decision #5: SHA-256 computed from actual written bytes; post-write ``FileStamp``
  is verified against those exact bytes before registering into ``turn_deliverables``.
  If an overwrite happens between write return and callback read, the file is NOT
  registered (and any prior entry for that path is evicted) so only a web login link is sent.
- Lead Decision #6: ``download_verified_deliverable`` verifies the in-flight DM delivery's
  pinned ``dm_recipient_platform_user_id`` and live platform binding (Weixin uses
  ``preflight_weixin_send``; Feishu/Discord check live roster + ``dalton_channel_identities``;
  background runs without an original DM recipient record are refused with 403).
- Online sandbox versioned read (``read_contained_sandbox_file_versioned``) and offline
  backup fallback (``workspace_files.sandbox_modified_at`` + ``content_hash`` / ``blob_sha256``),
  with UTF-8 credential redaction (``redact_bytes``) on text deliverables.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import posixpath
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.dalton.capabilities import (
    CapabilityId,
    has_capability,
    require_channel_platform_capability,
)
from src.dalton.colleague_outbound_guard import (
    is_run_outbound_blocked,
    is_run_row_outbound_blocked,
)
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import weixin_multi_account as wx_db
from src.ptc_agent.core.sandbox.file_version import FileStamp
from src.server.database.pool import get_db_connection
from src.server.services.workspace_file_containment import (
    FileTooLargeToServe,
    contained_absolute_path,
    is_within,
    read_contained_sandbox_file_versioned,
)
from src.server.utils.secret_redactor import (
    get_redactor,
    get_vault_secrets_for_redaction,
)

logger = logging.getLogger(__name__)

MAX_DELIVERABLE_BYTES = 20 * 1024 * 1024  # 20 MB

DELIVERABLE_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {
        # Documents
        ".pdf",
        ".docx",
        ".doc",
        ".md",
        ".markdown",
        ".txt",
        ".rtf",
        ".odt",
        # Spreadsheets
        ".xlsx",
        ".xlsm",
        ".xls",
        ".csv",
        ".tsv",
        # Presentations
        ".pptx",
        ".ppt",
        # Raster images
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
    }
)

DELIVERABLE_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".agents",
        "_internal",
        "uploads",
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        ".pytest_cache",
    }
)

DELIVERABLE_EXCLUDED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".html",
        ".htm",
        ".svg",
        ".py",
        ".sh",
        ".bash",
        ".zsh",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".sql",
        ".ipynb",
        ".r",
        ".go",
        ".rs",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".lock",
        ".log",
        ".pem",
        ".key",
        ".crt",
        ".parquet",
        ".feather",
        ".arrow",
        ".pkl",
        ".pickle",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".duckdb",
        ".h5",
        ".npz",
        ".zip",
        ".tar",
        ".gz",
        ".tgz",
        ".7z",
    }
)

_TEXT_REDACT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".csv",
        ".tsv",
        ".rtf",
    }
)

_MIME_OVERRIDES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".rtf": "application/rtf",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv; charset=utf-8",
    ".tsv": "text/tab-separated-values; charset=utf-8",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def normalize_deliverable_rel_path(
    raw_path: str | None,
    *,
    work_dir: str | None = None,
) -> str | None:
    """Normalize a workspace-relative (or work_dir-rooted) path into a clean relative path.

    Rejects control characters, ``..`` traversal, hidden segments, excluded directories,
    or paths escaping ``work_dir``.
    """
    if not isinstance(raw_path, str):
        return None
    cleaned = raw_path.strip().replace("\\", "/")
    if not cleaned or _has_control_chars(cleaned):
        return None

    if work_dir:
        wd = work_dir.strip().rstrip("/")
        if cleaned == wd:
            return None
        if cleaned.startswith(wd + "/"):
            cleaned = cleaned[len(wd) + 1 :]
    if cleaned.startswith("/"):
        return None

    # Reject explicit `..` before normalization so traversal cannot hide behind folding.
    raw_parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if not raw_parts:
        return None
    for part in raw_parts:
        if part == ".." or part.startswith(".") or part in DELIVERABLE_EXCLUDED_DIRS:
            return None

    normalized = posixpath.normpath("/".join(raw_parts))
    if not normalized or normalized in (".", "..") or normalized.startswith("../"):
        return None
    return normalized


def _check_single_path_allowlist(rel_path: str) -> tuple[bool, str | None]:
    parts = rel_path.split("/")
    for part in parts:
        if not part or part in (".", ".."):
            return False, "invalid_path_segment"
        if part.startswith(".") or part in DELIVERABLE_EXCLUDED_DIRS:
            return False, "excluded_directory_or_hidden"
    _, ext = os.path.splitext(parts[-1])
    ext_lower = ext.lower()
    if not ext_lower or ext_lower in DELIVERABLE_EXCLUDED_EXTENSIONS:
        return False, "excluded_extension"
    if ext_lower not in DELIVERABLE_ALLOWED_EXTENSIONS:
        return False, "extension_not_allowed"
    return True, None


def is_deliverable_path_allowed(
    requested_path: str | None,
    canonical_path: str | None = None,
    *,
    work_dir: str | None = None,
) -> tuple[bool, str | None]:
    """Enforce §4.3 dual-path allowlist on both requested path and resolved realpath."""
    rel = normalize_deliverable_rel_path(requested_path, work_dir=work_dir)
    if rel is None:
        return False, "invalid_requested_path"
    ok_req, reason_req = _check_single_path_allowlist(rel)
    if not ok_req:
        return False, reason_req

    if canonical_path is not None:
        if work_dir:
            wd = work_dir.strip().rstrip("/")
            if not is_within(wd, canonical_path):
                return False, "canonical_outside_workspace"
        canon_rel = normalize_deliverable_rel_path(canonical_path, work_dir=work_dir)
        if canon_rel is None:
            return False, "invalid_canonical_path"
        ok_canon, reason_canon = _check_single_path_allowlist(canon_rel)
        if not ok_canon:
            return False, reason_canon

    return True, None


def validate_deliverable_magic_bytes(path: str, data: bytes) -> bool:
    """Verify binary header magic bytes for strictly typed binary deliverable formats."""
    _, ext = os.path.splitext(str(path or "").strip())
    ext_lower = ext.lower()
    if ext_lower == ".pdf":
        return data.startswith(b"%PDF-")
    if ext_lower == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext_lower in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if ext_lower == ".gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if ext_lower == ".webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if ext_lower == ".bmp":
        return data.startswith(b"BM")
    if ext_lower in (".docx", ".xlsx", ".xlsm", ".pptx", ".odt"):
        return data.startswith((b"PK\x03\x04", b"PK\x05\x06"))
    return True


def guess_deliverable_mime_type(path: str) -> str:
    _, ext = os.path.splitext(str(path or "").strip())
    ext_lower = ext.lower()
    if ext_lower in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext_lower]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _stamp_to_dict(stamp: FileStamp) -> dict[str, Any]:
    return {
        "size": int(stamp.size),
        "mtime": str(stamp.mtime),
        "ctime": str(stamp.ctime),
        "inode": str(stamp.inode),
    }


def _stamps_match(recorded: dict[str, Any] | None, current: FileStamp | None) -> bool:
    if not isinstance(recorded, dict) or current is None:
        return False
    try:
        return (
            int(recorded.get("size")) == int(current.size)
            and str(recorded.get("mtime")) == str(current.mtime)
            and str(recorded.get("ctime")) == str(current.ctime)
            and str(recorded.get("inode")) == str(current.inode)
        )
    except (TypeError, ValueError):
        return False


def _mtime_str_to_micros(mtime_str: str | None) -> int | None:
    if not mtime_str:
        return None
    try:
        dec = Decimal(str(mtime_str).strip())
        return int(dec * Decimal(1_000_000))
    except (InvalidOperation, ValueError):
        return None


class TurnDeliverableRecorder:
    """Per-run isolated recorder for Write/Edit tool writes to the user's own workspace.

    Implements Lead Decision #5:
    - Computes SHA-256 from the actual bytes written by the tool call.
    - Performs a post-write versioned contained read (``read_contained_sandbox_file_versioned``)
      to obtain ``FileStamp`` and verifies the read bytes match ``written_sha256`` and
      ``written_size``.
    - If another writer overwrote the file between write return and callback read (or
      the stamp cannot be verified), the file is NOT registered and any prior entry for
      that path is evicted so the gateway falls back to a web login deep link.
    """

    def __init__(
        self,
        *,
        thread_id: str,
        run_id: str,
        workspace_id: str,
        sandbox: Any,
        work_dir: str,
        versioned_reader: Callable[..., Awaitable[Any]] = read_contained_sandbox_file_versioned,
    ) -> None:
        self.thread_id = str(thread_id)
        self.run_id = str(run_id)
        self.workspace_id = str(workspace_id)
        self.sandbox = sandbox
        self.work_dir = str(work_dir).rstrip("/")
        self._versioned_reader = versioned_reader
        self._records: dict[str, dict[str, Any]] = {}

    async def on_file_operation(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        op = str(event.get("operation") or "").strip().lower()
        if op not in ("write_file", "edit_file"):
            return

        raw_path = event.get("file_path")
        rel_path = normalize_deliverable_rel_path(
            str(raw_path or ""), work_dir=self.work_dir
        )
        if rel_path is None:
            return

        allowed_req, _ = is_deliverable_path_allowed(rel_path, work_dir=self.work_dir)
        if not allowed_req:
            self._records.pop(rel_path, None)
            return

        written_sha256 = str(event.get("written_sha256") or "").strip().lower()
        written_size_raw = event.get("written_size")
        written_size: int | None = (
            int(written_size_raw) if isinstance(written_size_raw, int) else None
        )

        # For write_file, allow computing from exact passed `content` if caller omitted pre-hash.
        # For edit_file, NEVER compute from post-write re-read `content` (Lead Decision #5).
        if op == "write_file" and (not written_sha256 or written_size is None):
            raw_content = event.get("content")
            if isinstance(raw_content, str):
                encoded = raw_content.encode("utf-8")
                written_sha256 = hashlib.sha256(encoded).hexdigest()
                written_size = len(encoded)

        if not written_sha256 or written_size is None or written_size < 0:
            self._records.pop(rel_path, None)
            return

        if written_size > MAX_DELIVERABLE_BYTES:
            self._records.pop(rel_path, None)
            return

        abs_path = contained_absolute_path(rel_path, self.work_dir)
        if abs_path is None or self.sandbox is None:
            self._records.pop(rel_path, None)
            return

        try:
            contained = await self._versioned_reader(
                self.sandbox, abs_path, work_dir=self.work_dir
            )
        except FileTooLargeToServe:
            self._records.pop(rel_path, None)
            return
        except Exception as exc:
            logger.debug(
                "[channel_deliverables] post-write versioned read failed for %s: %s",
                rel_path,
                exc,
            )
            self._records.pop(rel_path, None)
            return

        if (
            contained is None
            or contained.content is None
            or contained.stamp is None
        ):
            self._records.pop(rel_path, None)
            return

        allowed_dual, _ = is_deliverable_path_allowed(
            rel_path, contained.canonical, work_dir=self.work_dir
        )
        if not allowed_dual:
            self._records.pop(rel_path, None)
            return

        read_bytes = contained.content
        read_sha256 = hashlib.sha256(read_bytes).hexdigest()
        if (
            len(read_bytes) != written_size
            or read_sha256 != written_sha256
            or int(contained.stamp.size) != written_size
        ):
            # Overwritten between write return and callback read -> cannot confirm stamp!
            self._records.pop(rel_path, None)
            return

        self._records[rel_path] = {
            "workspace_id": self.workspace_id,
            "path": rel_path,
            "size": written_size,
            "sha256": written_sha256,
            "stamp": _stamp_to_dict(contained.stamp),
            "stamp_settled": bool(contained.settled),
        }

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._records.values()]


def create_turn_deliverable_recorder(
    *,
    thread_id: str | None,
    run_id: str | None,
    workspace_id: str | None,
    user_id: str | None,
    workspace_owner_id: str | None,
    shared: bool,
    sandbox: Any,
    work_dir: str | None,
) -> TurnDeliverableRecorder | None:
    """Instantiate a ``TurnDeliverableRecorder`` when CHANNEL_GATEWAY is active on user's own workspace."""
    if not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return None
    if shared:
        return None
    uid = str(user_id or "").strip()
    ws_owner = str(workspace_owner_id or "").strip()
    ws_id = str(workspace_id or "").strip()
    tid = str(thread_id or "").strip()
    rid = str(run_id or "").strip()
    if not uid or not ws_owner or uid != ws_owner or not ws_id or not tid or not rid:
        return None
    if sandbox is None or not work_dir:
        return None
    return TurnDeliverableRecorder(
        thread_id=tid,
        run_id=rid,
        workspace_id=ws_id,
        sandbox=sandbox,
        work_dir=work_dir,
    )


async def collect_persistable_turn_deliverables(
    recorder: TurnDeliverableRecorder | None,
    *,
    run_id: str,
    user_id: str | None,
) -> list[dict[str, Any]]:
    """Return recorded deliverables iff CHANNEL_GATEWAY is enabled and run is not outbound-blocked."""
    if recorder is None or not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return []
    items = recorder.snapshot()
    if not items:
        return []
    blocked = await is_run_outbound_blocked(
        {"configurable": {"run_id": run_id, "user_id": user_id or ""}}
    )
    if blocked:
        return []
    return items


async def _default_get_live_sandbox(workspace_id: str) -> tuple[Any | None, str | None]:
    """Return ``(sandbox, work_dir)`` if the workspace's sandbox is currently running, else ``(None, None)``."""
    try:
        from src.server.services.workspace_manager import WorkspaceManager

        ws_mgr = WorkspaceManager.get_instance()
        session = ws_mgr.get_cached_session(workspace_id)
        if session is None:
            return None, None
        sandbox = getattr(session, "sandbox", None)
        if sandbox is None:
            return None, None
        work_dir = getattr(sandbox, "working_directory", None)
        if not work_dir:
            cfg = getattr(sandbox, "config", None)
            fs_cfg = getattr(cfg, "filesystem", None)
            work_dir = getattr(fs_cfg, "working_directory", None)
        if not work_dir:
            return None, None
        return sandbox, str(work_dir).rstrip("/")
    except Exception:
        return None, None


async def _default_read_offline_backup(
    workspace_id: str,
    rel_path: str,
    *,
    user_id: str,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Read offline ``workspace_files`` metadata and raw bytes when sandbox is stopped."""
    from src.server.app.workspace_files._shared import http_file_bytes
    from src.server.database import workspace_file as wf_db

    file_rec = await wf_db.get_file(
        workspace_id, rel_path, include_content=True, conn=conn
    )
    if not file_rec or str(file_rec.get("kind") or "file") != "file":
        return None
    locator = await wf_db.get_file_locator(workspace_id, rel_path, conn=conn)
    raw_bytes = await http_file_bytes(file_rec, user_id=user_id)
    return {
        "file_size": file_rec.get("file_size"),
        "content_hash": file_rec.get("content_hash"),
        "blob_sha256": file_rec.get("blob_sha256"),
        "mtime_ns": locator.get("mtime_ns") if locator else None,
        "content": raw_bytes,
    }


async def download_verified_deliverable(
    *,
    delivery_key: str,
    delivery_attempt: int,
    workspace_id: str,
    path: str,
    gateway_epoch: int | str | None,
    conn: Any = None,
    get_live_sandbox_fn: Callable[[str], Awaitable[tuple[Any | None, str | None]]] | None = None,
    versioned_reader_fn: Callable[..., Awaitable[Any]] = read_contained_sandbox_file_versioned,
    offline_backup_reader_fn: Callable[..., Awaitable[dict[str, Any] | None]] | None = None,
) -> tuple[bytes, str, str]:
    """Verify recipient, binding, ownership, and FileStamp/SHA-256 before returning DM deliverable bytes.

    Returns ``(redacted_bytes, mime_type, filename)``.
    Raises:
    - 404 when capability disabled, delivery missing, or file not in ``turn_deliverables``
    - 403 ``deliverable_recipient_denied`` on DM/recipient/binding/ownership/outbound-guard failure
    - 409 ``deliverable_version_mismatch`` when current FileStamp or SHA-256 differs from recorded turn write
    """
    cleaned_key = str(delivery_key or "").strip()
    cleaned_ws = str(workspace_id or "").strip()
    norm_rel = normalize_deliverable_rel_path(path)
    if not cleaned_key or not cleaned_ws or not norm_rel:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_deliverable_request",
                "message": "delivery_key, workspace_id, and valid relative path are required.",
            },
        )

    allowed_req, req_reason = is_deliverable_path_allowed(norm_rel)
    if not allowed_req:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "deliverable_path_not_allowed",
                "message": f"Deliverable path rejected: {req_reason}",
            },
        )

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT delivery_key, kind, user_id, thread_id::text AS thread_id,
                       run_id, is_current, platform, target_address, status,
                       delivery_attempt, dm_recipient_platform_user_id,
                       weixin_account_id::text AS weixin_account_id,
                       weixin_bot_generation, weixin_recipient_id
                FROM dalton_channel_deliveries
                WHERE delivery_key = %s
                """,
                (cleaned_key,),
            )
            d_row = await cur.fetchone()
            if not d_row:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "delivery_not_found", "message": "Delivery not found."},
                )
            d_row = dict(d_row)
            platform = str(d_row.get("platform") or "").strip().lower()
            # Platform sub-capability check BEFORE epoch parsing (R7 Decision #8)
            require_channel_platform_capability(platform)

            if str(d_row.get("kind") or "").strip().lower() not in ("agent_send", "fallback"):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "deliverable_recipient_denied",
                        "message": "Deliverables download is only allowed for conversation deliveries.",
                    },
                )

            # Verify gateway epoch + in_flight + attempt + DM + live binding + pinned recipient
            if platform == "weixin":
                await wx_db.preflight_weixin_send(
                    delivery_key=cleaned_key,
                    gateway_epoch=gateway_epoch,
                    delivery_attempt=delivery_attempt,
                    conn=owned_conn,
                )
                if not bool(d_row.get("is_current")) or not cg_db._is_dm_address(
                    platform, str(d_row.get("target_address") or "")
                ):
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "deliverable_recipient_denied",
                            "message": "Deliverables require a current DM delivery.",
                        },
                    )
            else:
                await cg_db.verify_dm_delivery_recipient(
                    delivery_key=cleaned_key,
                    delivery_attempt=delivery_attempt,
                    gateway_epoch=gateway_epoch,
                    require_gateway_epoch=True,
                    conn=owned_conn,
                )

            user_id = str(d_row.get("user_id") or "").strip()
            thread_id = str(d_row.get("thread_id") or "").strip()
            run_id = str(d_row.get("run_id") or "").strip()
            if not user_id or not thread_id or not run_id:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "deliverable_recipient_denied",
                        "message": "Delivery is missing user_id, thread_id, or run_id.",
                    },
                )

            # Verify run belongs to thread and is not outbound-blocked
            await cur.execute(
                """
                SELECT conversation_response_id, conversation_thread_id::text AS thread_id,
                       metadata
                FROM conversation_responses
                WHERE conversation_response_id = %s
                """,
                (run_id,),
            )
            run_row = await cur.fetchone()
            if not run_row or str(run_row.get("thread_id") or "") != thread_id:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "deliverable_not_found", "message": "Run record not found."},
                )
            run_row = dict(run_row)
            if is_run_row_outbound_blocked(run_row):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "deliverable_outbound_blocked",
                        "message": "Run is outbound-blocked for file delivery.",
                    },
                )

            # Verify workspace is owned by the initiating user (not shared)
            await cur.execute(
                """
                SELECT workspace_id::text AS workspace_id, user_id
                FROM workspaces
                WHERE workspace_id = %s::uuid
                """,
                (cleaned_ws,),
            )
            ws_row = await cur.fetchone()
            if not ws_row or str(ws_row.get("user_id") or "").strip() != user_id:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "deliverable_recipient_denied",
                        "message": "Deliverables are limited to the user's own workspace.",
                    },
                )

            # Find recorded deliverable entry in conversation_responses.metadata.turn_deliverables
            run_meta = run_row.get("metadata")
            meta_map = run_meta if isinstance(run_meta, dict) else {}
            raw_deliverables = meta_map.get("turn_deliverables")
            entry: dict[str, Any] | None = None
            if isinstance(raw_deliverables, list):
                for item in raw_deliverables:
                    if not isinstance(item, dict):
                        continue
                    item_ws = str(item.get("workspace_id") or "").strip()
                    item_path = normalize_deliverable_rel_path(item.get("path"))
                    if item_ws == cleaned_ws and item_path == norm_rel:
                        entry = item
                        break
            if not entry or not str(entry.get("sha256") or "").strip():
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "deliverable_not_found",
                        "message": "File was not recorded as a verified deliverable for this turn.",
                    },
                )

            expected_sha256 = str(entry["sha256"]).strip().lower()
            expected_size = int(entry.get("size") or 0)
            recorded_stamp = entry.get("stamp") if isinstance(entry.get("stamp"), dict) else None
            if expected_size <= 0 or expected_size > MAX_DELIVERABLE_BYTES:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "deliverable_version_mismatch",
                        "message": "Recorded deliverable size is invalid or exceeds limit.",
                    },
                )

            # Read file online if sandbox is running, otherwise offline backup from workspace_files
            live_getter = get_live_sandbox_fn or _default_get_live_sandbox
            sandbox, work_dir = await live_getter(cleaned_ws)

            raw_bytes: bytes | None = None
            if sandbox is not None and work_dir:
                abs_path = contained_absolute_path(norm_rel, work_dir)
                if abs_path is None:
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "deliverable_path_not_allowed",
                            "message": "Path escapes workspace containment.",
                        },
                    )
                try:
                    contained = await versioned_reader_fn(
                        sandbox, abs_path, work_dir=work_dir
                    )
                except FileTooLargeToServe as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "deliverable_version_mismatch",
                            "message": "File exceeds maximum deliverable size.",
                        },
                    ) from exc

                if (
                    contained is None
                    or contained.content is None
                    or contained.stamp is None
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "deliverable_version_mismatch",
                            "message": "File missing or modified during read.",
                        },
                    )
                ok_dual, reason_dual = is_deliverable_path_allowed(
                    norm_rel, contained.canonical, work_dir=work_dir
                )
                if not ok_dual:
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "deliverable_path_not_allowed",
                            "message": f"Resolved path rejected: {reason_dual}",
                        },
                    )
                if (
                    len(contained.content) != expected_size
                    or hashlib.sha256(contained.content).hexdigest() != expected_sha256
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "deliverable_version_mismatch",
                            "message": "File content changed after tool write.",
                        },
                    )
                if recorded_stamp is not None and not _stamps_match(
                    recorded_stamp, contained.stamp
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "deliverable_version_mismatch",
                            "message": "FileStamp changed after tool write.",
                        },
                    )
                raw_bytes = contained.content
            else:
                offline_reader = offline_backup_reader_fn or _default_read_offline_backup
                backup = await offline_reader(
                    cleaned_ws, norm_rel, user_id=user_id, conn=owned_conn
                )
                if not backup or not isinstance(backup.get("content"), (bytes, bytearray)):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "deliverable_version_mismatch",
                            "message": "Offline backup missing for deliverable.",
                        },
                    )
                b_bytes = bytes(backup["content"])
                b_size = int(backup.get("file_size") or len(b_bytes))
                b_hash = str(
                    backup.get("content_hash") or backup.get("blob_sha256") or ""
                ).strip().lower()
                if (
                    b_size != expected_size
                    or len(b_bytes) != expected_size
                    or hashlib.sha256(b_bytes).hexdigest() != expected_sha256
                    or (b_hash and b_hash != expected_sha256)
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "deliverable_version_mismatch",
                            "message": "Offline backup hash or size does not match recorded turn deliverable.",
                        },
                    )
                if recorded_stamp is not None:
                    rec_micros = _mtime_str_to_micros(recorded_stamp.get("mtime"))
                    b_mtime_ns = backup.get("mtime_ns")
                    b_micros = (
                        int(b_mtime_ns) // 1000
                        if isinstance(b_mtime_ns, int)
                        else None
                    )
                    if rec_micros is None or b_micros is None or rec_micros != b_micros:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "deliverable_version_mismatch",
                                "message": "Offline backup timestamp does not match recorded FileStamp.",
                            },
                        )
                raw_bytes = b_bytes

    if raw_bytes is None or len(raw_bytes) > MAX_DELIVERABLE_BYTES:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "deliverable_version_mismatch",
                "message": "Deliverable exceeds size limit.",
            },
        )

    if not validate_deliverable_magic_bytes(norm_rel, raw_bytes):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "deliverable_magic_mismatch",
                "message": "Deliverable file header does not match its extension.",
            },
        )

    _, ext = os.path.splitext(norm_rel)
    out_bytes = raw_bytes
    if ext.lower() in _TEXT_REDACT_EXTENSIONS:
        vault_secrets = await get_vault_secrets_for_redaction(user_id)
        out_bytes = get_redactor().redact_bytes(
            raw_bytes, encoding="utf-8", vault_secrets=vault_secrets
        )

    filename = posixpath.basename(norm_rel) or "attachment"
    mime_type = guess_deliverable_mime_type(norm_rel)
    return out_bytes, mime_type, filename
