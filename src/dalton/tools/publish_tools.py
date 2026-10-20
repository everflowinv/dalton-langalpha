"""Direct LangChain tools for Dalton publication governance: check_draft and publish_report (PK.9, PK.11)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.config.env import LOCAL_DEV_USER_ID
from src.dalton.database.publications import resolve_thread_author_model
from src.dalton.publish.service import run_check_draft, run_publish
from src.dalton.tools.knowledge_tools import (
    _extract_run_identity,
    _resolve_thread_origin,
)
from src.tools.secretary._commands import InjectedToolCallId

PublishKindLiteral = Literal[
    "initial_coverage",
    "full_coverage",
    "bullbear",
    "event_note",
    "earnings_preview",
    "earnings_review",
    "thesis_update",
    "topic",
    "conviction_call",
]


def create_publish_tools(filesystem_backend: Any = None) -> list[BaseTool]:
    """Build ``[check_draft, publish_report]`` bound to ``filesystem_backend``."""

    @tool("check_draft")
    async def _check_draft(
        draft_path: str,
        facts_path: str | None = None,
        *,
        config: RunnableConfig,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str:
        """Audit every figure in a draft against evidence, model cells, and provenance without blocking.

        Writes drafts/<slug>.audit.md and drafts/<slug>.annotated.md so you can fix unverified figures before calling publish_report.
        Returns: one-line summary of total, matched, exempt, and unverified figures plus the audit file path.
        """
        user_id, workspace_id, thread_id, _ = _extract_run_identity(config)
        if not workspace_id:
            return "ERROR: workspace_id is required in RunnableConfig."
        res = await run_check_draft(
            workspace_id=workspace_id,
            draft_path=draft_path,
            facts_path=facts_path,
            backend=filesystem_backend,
            use_llm_judge=True,
            user_id=user_id or LOCAL_DEV_USER_ID,
            thread_id=thread_id,
        )
        return str(res["message"])

    @tool("publish_report")
    async def _publish_report(
        draft_path: str,
        title: str,
        kind: PublishKindLiteral,
        slug: str,
        attachments: list[dict[str, Any]] | None = None,
        updates_thesis: bool = False,
        request_id: str | None = None,
        *,
        config: RunnableConfig,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str:
        """Publish a finished draft as the fund's official research (.md and optional .xlsx model): audits every figure, gets an independent review, then asks the PM to approve.

        Call only when the draft is final; while drafting use check_draft. Unmatched figures or a "revise" review come back as a list to fix — fix and call again.
        Returns: the published paths, or what blocks publishing and what to do next.
        """
        user_id, workspace_id, thread_id, _ = _extract_run_identity(config)
        if not user_id or not workspace_id or not thread_id:
            return "ERROR: user_id, workspace_id, and thread_id are required in RunnableConfig."

        cfg = (config or {}).get("configurable") or {}
        author_model = (
            cfg.get("served_model")
            or cfg.get("model")
            or cfg.get("llm_model")
        )
        if not author_model:
            author_model = await resolve_thread_author_model(thread_id)
        origin_type, _ = await _resolve_thread_origin(thread_id)

        cleaned_attachments = [
            a
            for a in (attachments or [])
            if isinstance(a, dict) and str(a.get("role") or "") != "pdf_source"
        ]

        res = await run_publish(
            user_id=user_id,
            workspace_id=workspace_id,
            thread_id=thread_id,
            tool_call_id=tool_call_id or "publish_report",
            draft_path=draft_path,
            title=title,
            kind=kind,
            slug=slug,
            attachments=cleaned_attachments,
            updates_thesis=updates_thesis,
            request_id=request_id,
            backend=filesystem_backend,
            author_model=str(author_model) if author_model else None,
            origin_type=origin_type,
        )
        return str(res["message"])

    return [_check_draft, _publish_report]


def publish_tools(filesystem_backend: Any = None) -> list[BaseTool]:
    """Return the publication governance tools bound to ``filesystem_backend``."""
    return create_publish_tools(filesystem_backend)


_DEFAULT_PUBLISH_TOOLS = create_publish_tools(None)
check_draft = _DEFAULT_PUBLISH_TOOLS[0]
publish_report = _DEFAULT_PUBLISH_TOOLS[1]
