"""Backend tools `podcast_weekly_read` and `podcast_weekly_write` (internal/admin only)."""

from __future__ import annotations

import json
import logging
from typing import Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.access_tiers import is_internal_or_admin
from src.dalton.podcast_weekly_store import (
    PodcastStoreSecurityError,
    RecordUnreadableError,
    abandon_pending_report,
    get_status,
    mark_reported,
    prepare_doc,
    read_artifact,
    record_chunk,
    record_failure,
    resolve_host_openclaw_data_root,
    save_doc,
    save_pending_report,
    write_artifact,
)
from src.dalton.tools.conversation_tools import _extract_caller_identity

logger = logging.getLogger(__name__)


async def _authorize_internal(tool_name: str, config: RunnableConfig | None) -> str | None:
    user_id, workspace_id, thread_id, _run_id = _extract_caller_identity(config)
    if not user_id:
        logger.warning(
            "[dalton.podcast_weekly] rejected unauthenticated %s (workspace_id=%s thread_id=%s)",
            tool_name,
            workspace_id or "-",
            thread_id or "-",
        )
        return json.dumps(
            {
                "ok": False,
                "error": "unauthenticated",
                "message": f"{tool_name} requires an authenticated internal or admin user.",
            },
            ensure_ascii=False,
        )
    if not await is_internal_or_admin(user_id):
        logger.warning(
            "[dalton.podcast_weekly] rejected external %s (user_id=%s)",
            tool_name,
            user_id,
        )
        return json.dumps(
            {
                "ok": False,
                "error": "not_allowed",
                "message": f"{tool_name} is restricted to internal and admin members.",
            },
            ensure_ascii=False,
        )
    return None


def create_podcast_weekly_tools() -> list[BaseTool]:
    """Build `[podcast_weekly_read, podcast_weekly_write]` (`DALTON-HOOK(dalton-tools)`)."""

    @tool("podcast_weekly_read")
    async def podcast_weekly_read(
        action: str,
        video_id: Optional[str] = None,
        artifact: Optional[str] = None,
        date: Optional[str] = None,
        pending_only: bool = False,
        offset: int = 0,
        max_chars: int = 16000,
        *,
        config: RunnableConfig,
    ) -> str:
        """Read host-side `podcast-weekly` ledger status, episode artifacts, or chunked Feishu doc payloads.

        Internal and admin users only.

        Args:
            action: `"status"` (compute `next_action`, `to_publish`, `report_success`, `report_failed`),
                `"read_artifact"` (paginated episode file read via `offset`/`max_chars`), or
                `"prepare_doc"` (build or resume Feishu doc chunks for `video_id`).
            video_id: Required for `"read_artifact"` and `"prepare_doc"` (`^[A-Za-z0-9_-]{3,64}$`).
            artifact: Required for `"read_artifact"`: `"zh"`, `"en"`, `"notes"`, `"merged"`,
                `"summary"`, or `"doc_state"`.
            date: Optional `YYYY-MM-DD` (HKT) date override for `"status"` or `"prepare_doc"`.
            pending_only: When true with `"status"`, omit already-published completed episodes.
            offset: Starting character offset for `"read_artifact"` (default 0).
            max_chars: Max characters per `"read_artifact"` page (1-32000, default 16000), or
                chunk limit for `"prepare_doc"` (uses 25000 at default `16000`; min 1000).

        Returns:
            JSON with `ok` and `status` queues/`next_action`, paginated `content`, or Feishu `chunks`/`remaining_chunks`.

        Stop on `ok=false`; never treat `ledger_missing` or `ledger_unreadable` as an empty week.
        """
        denied = await _authorize_internal("podcast_weekly_read", config)
        if denied is not None:
            return denied

        act = str(action or "").strip().lower()
        root = resolve_host_openclaw_data_root()
        try:
            if act == "status":
                out = get_status(root, date_str=date, pending_only=bool(pending_only))
                return json.dumps(out, ensure_ascii=False)
            if act == "read_artifact":
                if not video_id or not artifact:
                    return json.dumps(
                        {"ok": False, "error": "missing_params", "message": "需要 video_id 与 artifact"},
                        ensure_ascii=False,
                    )
                out = read_artifact(
                    root,
                    video_id,
                    artifact,
                    offset=int(offset or 0),
                    max_chars=int(max_chars or 16000),
                )
                return json.dumps(out, ensure_ascii=False)
            if act == "prepare_doc":
                if not video_id:
                    return json.dumps(
                        {"ok": False, "error": "missing_video_id", "message": "prepare_doc 需要 video_id"},
                        ensure_ascii=False,
                    )
                chunk_limit = 25000 if max_chars == 16000 else max(1000, int(max_chars or 25000))
                out = prepare_doc(root, video_id, max_chars=chunk_limit, date_str=date)
                out.pop("markdown", None)
                return json.dumps(out, ensure_ascii=False)
            return json.dumps(
                {
                    "ok": False,
                    "error": "invalid_action",
                    "message": f"未知 action={action!r}，可选 status / read_artifact / prepare_doc",
                },
                ensure_ascii=False,
            )
        except (
            PodcastStoreSecurityError,
            RecordUnreadableError,
            FileNotFoundError,
            ValueError,
            OSError,
        ) as exc:
            return json.dumps(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            )

    @tool("podcast_weekly_write")
    async def podcast_weekly_write(
        action: str,
        video_id: Optional[str] = None,
        artifact: Optional[str] = None,
        content: Optional[str] = None,
        doc_url: Optional[str] = None,
        document_id: Optional[str] = None,
        zh_title: Optional[str] = None,
        part: Optional[int] = None,
        total_parts: Optional[int] = None,
        body_complete: Optional[bool] = None,
        error: Optional[str] = None,
        report_run_id: Optional[str] = None,
        text: Optional[str] = None,
        success_video_ids: Optional[list[str]] = None,
        failed_video_ids: Optional[list[str]] = None,
        empty_date: Optional[str] = None,
        date: Optional[str] = None,
        *,
        config: RunnableConfig,
    ) -> str:
        """Write proofread `podcast-weekly` artifacts and resumable Feishu publish or report state to host storage.

        Internal and admin users only.

        Args:
            action: `"write_artifact"` (save proofread file), `"save_doc"` (register new Feishu doc),
                `"record_chunk"` (mark appended chunk), `"record_failure"` (log doc creation failure),
                `"save_pending_report"` (persist summary before sending), `"abandon_pending_report"`
                (discard expired prior-day pending report), or `"mark_reported"` (record confirmed delivery).
            video_id: Episode `video_id` for episode actions (omitted for `"save_pending_report"`,
                `"abandon_pending_report"`, or `"mark_reported"`).
            artifact: Required for `"write_artifact"`: `"zh"`, `"en"`, `"notes"`, or `"summary"`.
            content: Full UTF-8 text content for `"write_artifact"`.
            doc_url: Feishu doc URL returned by `feishu_create_doc`, for `"save_doc"` or `"record_chunk"`.
            document_id: Feishu `document_id` returned by `feishu_create_doc`, for `"save_doc"` or `"record_chunk"`.
            zh_title: Translated Chinese episode title saved with `"save_doc"`.
            part: Completed chunk number (1-indexed, or `0` on `content_insert_failed`) for
                `"save_doc"` or `"record_chunk"`.
            total_parts: Total chunk count from `"prepare_doc"`, for `"save_doc"` or `"record_chunk"`.
            body_complete: Explicit completeness flag for `"save_doc"` (`False` when
                `content_insert_failed` or chunks remain).
            error: Failure detail for `"record_failure"`.
            report_run_id: Report run identifier for `"save_pending_report"`,
                `"abandon_pending_report"`, or `"mark_reported"`.
            text: Exact summary text saved by `"save_pending_report"` for deterministic resubmit.
            success_video_ids: Published `video_id` list for `"save_pending_report"` or
                `"mark_reported"`.
            failed_video_ids: Failed `video_id` list for `"save_pending_report"` or
                `"mark_reported"`.
            empty_date: HK `YYYY-MM-DD` date recorded in `empty_weeks` for empty-week
                `"save_pending_report"` or `"mark_reported"`.
            date: Optional HK `YYYY-MM-DD` date override when validating `"save_pending_report"`
                or `"abandon_pending_report"`.

        Returns:
            JSON with `ok` and updated artifact metadata, doc/chunk progress, or report state.

        Call `save_pending_report` before `automation_report`, and call `mark_reported` only after `delivery_confirmed=true`.
        """
        denied = await _authorize_internal("podcast_weekly_write", config)
        if denied is not None:
            return denied

        act = str(action or "").strip().lower()
        root = resolve_host_openclaw_data_root()
        try:
            if act == "write_artifact":
                if not video_id or not artifact or content is None:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_params",
                            "message": "write_artifact 需要 video_id, artifact, content",
                        },
                        ensure_ascii=False,
                    )
                out = write_artifact(root, video_id, artifact, content)
                return json.dumps(out, ensure_ascii=False)
            if act == "save_doc":
                if not video_id:
                    return json.dumps(
                        {"ok": False, "error": "missing_video_id", "message": "save_doc 需要 video_id"},
                        ensure_ascii=False,
                    )
                parts_list = [int(part)] if part is not None and int(part) > 0 else ([] if body_complete is False else None)
                out = save_doc(
                    root,
                    video_id,
                    doc_url=doc_url or "",
                    document_id=document_id or "",
                    zh_title=zh_title or "",
                    total_parts=int(total_parts or 1),
                    completed_parts=parts_list,
                    body_complete=body_complete,
                )
                return json.dumps(out, ensure_ascii=False)
            if act == "record_chunk":
                if not video_id or part is None:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_params",
                            "message": "record_chunk 需要 video_id 与 part",
                        },
                        ensure_ascii=False,
                    )
                out = record_chunk(
                    root,
                    video_id,
                    part=int(part),
                    total_parts=int(total_parts or 1),
                    document_id=document_id or "",
                    doc_url=doc_url or "",
                )
                return json.dumps(out, ensure_ascii=False)
            if act == "record_failure":
                if not video_id:
                    return json.dumps(
                        {"ok": False, "error": "missing_video_id", "message": "record_failure 需要 video_id"},
                        ensure_ascii=False,
                    )
                out = record_failure(root, video_id, error or "")
                return json.dumps(out, ensure_ascii=False)
            if act == "save_pending_report":
                if not report_run_id:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_report_run_id",
                            "message": "save_pending_report 需要 report_run_id",
                        },
                        ensure_ascii=False,
                    )
                report_text = text if text is not None else content
                if not isinstance(report_text, str) or not report_text.strip():
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_text",
                            "message": "save_pending_report 需要 text",
                        },
                        ensure_ascii=False,
                    )
                out = save_pending_report(
                    root,
                    report_run_id,
                    report_text,
                    success_ids=success_video_ids,
                    failed_ids=failed_video_ids,
                    empty_date=empty_date,
                    date_str=date,
                )
                return json.dumps(out, ensure_ascii=False)
            if act == "abandon_pending_report":
                if not report_run_id:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_report_run_id",
                            "message": "abandon_pending_report 需要 report_run_id",
                        },
                        ensure_ascii=False,
                    )
                out = abandon_pending_report(root, report_run_id, date_str=date)
                return json.dumps(out, ensure_ascii=False)
            if act == "mark_reported":
                if not report_run_id:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_report_run_id",
                            "message": "mark_reported 需要 report_run_id",
                        },
                        ensure_ascii=False,
                    )
                out = mark_reported(
                    root,
                    report_run_id,
                    success_ids=success_video_ids,
                    failed_ids=failed_video_ids,
                    empty_date=empty_date,
                )
                return json.dumps(out, ensure_ascii=False)
            return json.dumps(
                {
                    "ok": False,
                    "error": "invalid_action",
                    "message": (
                        f"未知 action={action!r}，可选 write_artifact / save_doc / "
                        "record_chunk / record_failure / save_pending_report / "
                        "abandon_pending_report / mark_reported"
                    ),
                },
                ensure_ascii=False,
            )
        except (
            PodcastStoreSecurityError,
            RecordUnreadableError,
            FileNotFoundError,
            ValueError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            return json.dumps(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            )

    return [podcast_weekly_read, podcast_weekly_write]
