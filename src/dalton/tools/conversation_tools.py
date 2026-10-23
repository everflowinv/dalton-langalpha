"""Direct LangChain tool `conversation_search` for the Dalton OpenClaw conversation archive."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.conversations.search import hybrid_search_conversations

logger = logging.getLogger(__name__)


def _extract_caller_identity(
    config: RunnableConfig | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Extract `(user_id, workspace_id, thread_id, run_id)` from `RunnableConfig`."""
    cfg = (config or {}).get("configurable") or {}
    user_id = str(cfg.get("user_id") or "").strip() or None
    workspace_id = str(cfg.get("workspace_id") or "").strip() or None
    thread_id = str(cfg.get("thread_id") or "").strip() or None
    run_id = str(cfg.get("run_id") or cfg.get("response_id") or "").strip() or None
    return user_id, workspace_id, thread_id, run_id


def create_conversation_tools() -> list[BaseTool]:
    """Build `[conversation_search]` for both Analyst and Chief of Staff roles."""

    @tool("conversation_search")
    async def _conversation_search(
        query: str,
        tickers: Optional[list[str]] = None,
        types: Optional[list[str]] = None,
        since: Optional[str] = None,
        speaker_role: Optional[str] = None,
        limit: int = 10,
        *,
        config: RunnableConfig,
    ) -> str:
        """Use when asked about any company, topic, or sector to check the team's prior views, model assumptions, decisions, and open todos first, or when verbatim quotes and OpenClaw provenance are needed.
        Covers `source_type=conversation` discussion cards only, not external filings or research documents; `limit` is capped at 50.

        Args:
            query: Natural-language question, company name, or keyword phrase.
            tickers: Optional list of uppercase ticker symbols to filter (e.g. `["NVDA", "TSM"]`).
            types: Optional card types (`fact`, `estimate`, `model_assumption`, `opinion`, `question`, `decision`, `todo`).
            since: Optional inclusive start date in `YYYY-MM-DD` (ET).
            speaker_role: Optional speaker filter (`owner`, `participant`, `agent`).
            limit: Maximum cards to return (1-50, default 10).

        Returns:
            JSON with ranked cards, verbatim `quote`, `figures`, and `openclaw://<agent>/<session_id>#<event_id>` provenance URIs.
        """
        user_id, workspace_id, thread_id, run_id = _extract_caller_identity(config)
        if not user_id:
            logger.warning(
                "[dalton.conversations.audit] rejected unauthenticated conversation_search call (workspace_id=%s thread_id=%s)",
                workspace_id or "-",
                thread_id or "-",
            )
            return json.dumps(
                {
                    "ok": False,
                    "error": "unauthenticated",
                    "message": "conversation_search requires an authenticated user turn (missing user_id in RunnableConfig).",
                },
                ensure_ascii=False,
            )

        result: dict[str, Any] = await hybrid_search_conversations(
            query=query,
            tickers=tickers,
            types=types,
            since=since,
            speaker_role=speaker_role,
            limit=limit,
        )
        for card in result.get("cards") or []:
            if not isinstance(card, dict):
                continue
            figs = card.get("figures")
            if isinstance(figs, list) and any(
                isinstance(f, dict) and str(f.get("metric") or "").strip() == "_verification"
                for f in figs
            ):
                card["verification"] = "unsupported_numbers"
        logger.info(
            "[dalton.conversations.audit] conversation_search caller_user_id=%s workspace_id=%s thread_id=%s run_id=%s count=%d",
            user_id,
            workspace_id or "-",
            thread_id or "-",
            run_id or "-",
            int(result.get("count") or 0),
        )
        return json.dumps(result, ensure_ascii=False, default=str)

    return [_conversation_search]


_DEFAULT_TOOLS = create_conversation_tools()
conversation_search = _DEFAULT_TOOLS[0]
