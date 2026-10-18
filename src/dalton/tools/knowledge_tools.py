"""Direct LangChain tools for the Dalton company knowledge layer (PK.4)."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.knowledge.reconcile import apply_pending
from src.dalton.knowledge.service import append_verdict, apply_update
from src.server.database.pool import get_db_connection
from src.tools.secretary._commands import InjectedToolCallId

logger = logging.getLogger(__name__)

DocLiteral = Literal[
    "company",
    "thesis",
    "debate",
    "drivers",
    "management",
    "lessons",
    "agenda",
    "models",
]


def _extract_run_identity(
    config: RunnableConfig | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Extract ``(user_id, workspace_id, thread_id, run_id)`` from ``RunnableConfig``."""
    cfg = (config or {}).get("configurable") or {}
    user_id = cfg.get("user_id")
    workspace_id = cfg.get("workspace_id")
    thread_id = cfg.get("thread_id")
    run_id = cfg.get("run_id") or cfg.get("response_id")
    return (
        str(user_id) if user_id else None,
        str(workspace_id) if workspace_id else None,
        str(thread_id) if thread_id else None,
        str(run_id) if run_id else None,
    )


async def _resolve_thread_origin(
    thread_id: str | None,
) -> tuple[str | None, str | None]:
    """Resolve ``(origin_type, automation_execution_id)`` for ``thread_id``."""
    if not thread_id:
        return None, None
    origin_type: str | None = None
    automation_execution_id: str | None = None
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT metadata->'origin'->>'type'
                    FROM conversation_threads
                    WHERE conversation_thread_id = %s
                    """,
                    (thread_id,),
                )
                row = await cur.fetchone()
                if row and row[0]:
                    origin_type = str(row[0])
                if origin_type == "automation":
                    await cur.execute(
                        """
                        SELECT automation_execution_id
                        FROM automation_executions
                        WHERE conversation_thread_id = %s
                        ORDER BY started_at DESC NULLS LAST
                        LIMIT 1
                        """,
                        (thread_id,),
                    )
                    erow = await cur.fetchone()
                    if erow and erow[0]:
                        automation_execution_id = str(erow[0])
    except Exception as e:
        logger.debug("Could not resolve thread origin for %s: %s", thread_id, e)
    return origin_type, automation_execution_id


def create_knowledge_tools(filesystem_backend: Any = None) -> list[BaseTool]:
    """Build ``[knowledge_update, knowledge_append_verdict]`` bound to ``filesystem_backend``."""
    reconciled_workspaces: set[str] = set()

    async def _maybe_reconcile(workspace_id: str, user_id: str) -> None:
        if filesystem_backend is None or workspace_id in reconciled_workspaces:
            return
        reconciled_workspaces.add(workspace_id)
        try:
            await apply_pending(workspace_id, filesystem_backend, user_id=user_id)
        except Exception as e:
            logger.debug("apply_pending skipped for %s: %s", workspace_id, e)

    @tool("knowledge_update")
    async def _knowledge_update(
        doc: DocLiteral,
        ops: list[dict[str, Any]],
        reason: str,
        base_version: int | None = None,
        evidence_refs: list[dict[str, Any]] | None = None,
        *,
        config: RunnableConfig,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str:
        """Change one company-knowledge document through validated operations; prefer this over editing data/*.json by hand.

        Read data/<doc>.json first and pass its version as base_version (null only to create the document).
        Ops address list items by id, e.g. {"op":"append","path":"/evidence","value":{...}},
        {"op":"set","path":"/pillars/P2/status","value":"watch"}. reason says why, in one or two sentences.
        Returns: the new version and the rendered file, or the list of problems to fix.
        """
        user_id, workspace_id, thread_id, run_id = _extract_run_identity(config)
        if not user_id or not workspace_id:
            return "ERROR: user_id and workspace_id are required in RunnableConfig."

        await _maybe_reconcile(workspace_id, user_id)
        res = await apply_update(
            user_id=user_id,
            workspace_id=workspace_id,
            doc_key=doc,
            ops=ops,
            reason=reason,
            base_version=base_version,
            evidence_refs=evidence_refs,
            source="tool",
            backend=filesystem_backend,
            thread_id=thread_id,
            run_id=run_id,
            tool_call_id=tool_call_id or None,
        )
        return str(res["message"])

    @tool("knowledge_append_verdict")
    async def _knowledge_append_verdict(
        verdict: dict[str, Any],
        agenda_ops: list[dict[str, Any]] | None = None,
        *,
        config: RunnableConfig,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str:
        """Record an event verdict (NO_CHANGE/THESIS_STRENGTHENED/THESIS_WEAKENED/THESIS_BROKEN/NEW_THESIS) and its next validation; every research session that looked at new evidence ends with one.

        Returns: the verdict id and the agenda version it updated.
        """
        user_id, workspace_id, thread_id, run_id = _extract_run_identity(config)
        if not user_id or not workspace_id:
            return "ERROR: user_id and workspace_id are required in RunnableConfig."

        await _maybe_reconcile(workspace_id, user_id)
        origin_type, auto_exec_id = await _resolve_thread_origin(thread_id)
        res = await append_verdict(
            user_id=user_id,
            workspace_id=workspace_id,
            verdict=verdict,
            agenda_ops=agenda_ops,
            backend=filesystem_backend,
            thread_id=thread_id,
            run_id=run_id,
            tool_call_id=tool_call_id or None,
            origin_type=origin_type,
            automation_execution_id=auto_exec_id,
        )
        return str(res["message"])

    return [_knowledge_update, _knowledge_append_verdict]


def knowledge_tools(filesystem_backend: Any = None) -> list[BaseTool]:
    """Return the company-knowledge tools bound to ``filesystem_backend``."""
    return create_knowledge_tools(filesystem_backend)


_DEFAULT_TOOLS = create_knowledge_tools(None)
knowledge_update = _DEFAULT_TOOLS[0]
knowledge_append_verdict = _DEFAULT_TOOLS[1]
