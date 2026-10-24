"""Unified Dalton tool builder (`build_dalton_tools`) for main agents (PK.4, PK.11, PK.15, PK.19)."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool

from src.dalton.settings import get_dalton_settings
from src.dalton.tools.director_tools import build_director_tools

logger = logging.getLogger(__name__)


def build_dalton_tools(
    role: str,
    filesystem_backend: Any = None,
    *,
    enabled: bool | None = None,
) -> list[BaseTool]:
    """Return role-appropriate Dalton tools to bind onto the main agent (`DALTON-HOOK(dalton-tools)`).

    - `chief_of_staff` (Home / Research Director):
      `dalton_coverage_board`, `dalton_record_plan`, `dalton_agenda_note`, `dalton_watch_event`, `dalton_resolve_signals`, `dalton_schedule_automation`, `dalton_coverage_tracking`, `dalton_system_jobs`, `conversation_search`, `dalton_check_done` (weekly-coverage-review only)
    - `analyst` (Company workspace):
      `knowledge_update`, `knowledge_append_verdict`, `dalton_check_done`, `check_draft`, `check_language`, `publish_report`, `dalton_schedule_automation`, `dalton_coverage_tracking`, `dalton_system_jobs`, `conversation_search`
    """
    settings = get_dalton_settings()
    is_enabled = enabled if enabled is not None else (
        settings.knowledge.enabled
        or settings.publish.enabled
        or settings.autonomy.enabled
        or settings.conversations.enabled
    )
    if not is_enabled:
        return []

    tools: list[BaseTool] = []

    if role == "chief_of_staff":
        if settings.autonomy.enabled:
            tools.extend(build_director_tools(role=role, filesystem_backend=filesystem_backend))
        if settings.conversations.enabled:
            try:
                from src.dalton.tools.conversation_tools import create_conversation_tools

                tools.extend(create_conversation_tools())
            except Exception as e:
                logger.debug("[DALTON_TOOLS] conversation_tools not loaded: %s", e)
        if settings.knowledge.enabled or settings.autonomy.enabled:
            try:
                # The weekly review closes in Home: only its contract is
                # checkable here (the role restriction lives in the tool).
                from src.dalton.tools.check_done_tools import create_check_done_tools

                tools.extend(
                    create_check_done_tools(
                        filesystem_backend=filesystem_backend, role="chief_of_staff"
                    )
                )
            except Exception as e:
                logger.debug("[DALTON_TOOLS] check_done_tools not loaded: %s", e)
        return tools

    # Analyst workspace tools
    if settings.knowledge.enabled:
        try:
            from src.dalton.tools.knowledge_tools import create_knowledge_tools

            tools.extend(create_knowledge_tools(filesystem_backend=filesystem_backend))
        except Exception as e:
            logger.debug("[DALTON_TOOLS] knowledge_tools not loaded: %s", e)

        try:
            # dalton_check_done rides the knowledge gate: its closing checks
            # read the knowledge version table and verdicts.
            from src.dalton.tools.check_done_tools import create_check_done_tools

            tools.extend(
                create_check_done_tools(filesystem_backend=filesystem_backend)
            )
        except Exception as e:
            logger.debug("[DALTON_TOOLS] check_done_tools not loaded: %s", e)

    if settings.publish.enabled:
        try:
            from src.dalton.tools.publish_tools import create_publish_tools

            tools.extend(create_publish_tools(filesystem_backend=filesystem_backend))
        except Exception as e:
            logger.debug("[DALTON_TOOLS] publish_tools not loaded: %s", e)

    if settings.autonomy.enabled:
        tools.extend(build_director_tools(role=role, filesystem_backend=filesystem_backend))

    if settings.conversations.enabled:
        try:
            from src.dalton.tools.conversation_tools import create_conversation_tools

            tools.extend(create_conversation_tools())
        except Exception as e:
            logger.debug("[DALTON_TOOLS] conversation_tools not loaded: %s", e)

    return tools
