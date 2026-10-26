"""Direct LangChain tools for Dalton publication governance: check_draft, check_language and publish_report (PK.9, PK.11)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.access_tiers import get_user_tier
from src.dalton.auth import resolve_caller_home_workspace_id
from src.dalton.database.publications import resolve_thread_author_model
from src.dalton.publish.language import run_check_language
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

EXTERNAL_PUBLISH_FORBIDDEN_MESSAGE = (
    "外部成员不能发布正式报告（publish_report 仅管理员与内部成员可用）；"
    "请如实告知用户这一身份限制，不要尝试绕过。"
)


def _build_check_language_tool(
    filesystem_backend: Any = None,
    *,
    allow_home_workspace: bool = False,
) -> BaseTool:
    """Build ``check_language`` bound to ``filesystem_backend``; only Chief of Staff
    (``allow_home_workspace=True``) resolves an omitted/``home`` workspace to Home."""

    @tool("check_language")
    async def _check_language(
        draft_path: str,
        round: int,
        *,
        config: RunnableConfig,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str:
        """审查正式产物草稿的语言问题，只标问题、不改稿，逐条给原句引用与修改方向。

        审查对象是给人读的 .md 正式产物（drafts/、research/ 下的笔记与草稿，含 research/push/ 下的推送稿）；json/xlsx 等机器文件、bg_data/、data/、lenses_work/、model_dump/ 下的抓取原文、知识层渲染文件（thesis.md 等）、research/topics/conversations/ 机器纪要、published/ 归档稿（其审查以对应草稿为准）与 .audit/.annotated/.language 侧车不审。round=1 在初稿写完、第一次独立审稿之前；round=2 在内容定稿后（一次写完的笔记即第 1 轮改完后），且要求已做过第 1 轮。每条 issue 的 quote 逐字摘自草稿，找不到的会被丢弃并计数。审查模型在 设置 → 模型 → 高级 → 语言审查 配置。改稿由你按清单自己做；本工具失败（模型不可用、超时）时不写侧车、不算通过。
        Returns: 一行摘要：轮次、问题数、丢弃的引用数与清单路径 <draft>.language.md。
        """
        user_id, workspace_id, thread_id, _ = _extract_run_identity(config)
        if not user_id:
            return "ERROR: user_id and workspace_id are required in RunnableConfig."
        if allow_home_workspace and (
            not workspace_id or workspace_id.strip().lower() == "home"
        ):
            try:
                workspace_id = await resolve_caller_home_workspace_id(user_id)
            except Exception as exc:
                detail = getattr(exc, "detail", None) or str(exc)
                return f"ERROR: {detail}"
        if not workspace_id or workspace_id.strip().lower() == "home":
            return "ERROR: user_id and workspace_id are required in RunnableConfig."
        res = await run_check_language(
            workspace_id=workspace_id,
            draft_path=draft_path,
            round=round,
            backend=filesystem_backend,
            user_id=user_id,
            thread_id=thread_id,
        )
        return str(res["message"])

    return _check_language


def create_publish_tools(filesystem_backend: Any = None) -> list[BaseTool]:
    """Build ``[check_draft, check_language, publish_report]`` bound to ``filesystem_backend``."""

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
        if not user_id or not workspace_id:
            return "ERROR: user_id and workspace_id are required in RunnableConfig."
        res = await run_check_draft(
            workspace_id=workspace_id,
            draft_path=draft_path,
            facts_path=facts_path,
            backend=filesystem_backend,
            use_llm_judge=True,
            user_id=user_id,
            thread_id=thread_id,
        )
        return str(res["message"])

    _check_language = _build_check_language_tool(
        filesystem_backend, allow_home_workspace=False
    )

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

        Admins and internal members only: external members are refused outright (report the refusal honestly; drafting and check_draft stay available).
        Call only when the draft is final; while drafting use check_draft. Unmatched figures or a "revise" review come back as a list to fix — fix and call again.
        Returns: the published paths, or what blocks publishing and what to do next.
        """
        user_id, workspace_id, thread_id, _ = _extract_run_identity(config)
        if not user_id or not workspace_id or not thread_id:
            return "ERROR: user_id, workspace_id, and thread_id are required in RunnableConfig."

        if await get_user_tier(user_id) == "external":
            return f"ERROR: {EXTERNAL_PUBLISH_FORBIDDEN_MESSAGE}"

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

    return [_check_draft, _check_language, _publish_report]


def create_language_tools(filesystem_backend: Any = None) -> list[BaseTool]:
    """Return ``[check_language]`` bound to ``filesystem_backend`` for Chief of Staff (Home)."""
    return [_build_check_language_tool(filesystem_backend, allow_home_workspace=True)]


def publish_tools(filesystem_backend: Any = None) -> list[BaseTool]:
    """Return the publication governance tools bound to ``filesystem_backend``."""
    return create_publish_tools(filesystem_backend)


_DEFAULT_PUBLISH_TOOLS = create_publish_tools(None)
check_draft = _DEFAULT_PUBLISH_TOOLS[0]
check_language = _DEFAULT_PUBLISH_TOOLS[1]
publish_report = _DEFAULT_PUBLISH_TOOLS[2]
