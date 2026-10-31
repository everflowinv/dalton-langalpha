"""Unified Dalton tool builder (`build_dalton_tools`) for main agents (PK.4, PK.11, PK.15, PK.19)."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool

from src.dalton.settings import get_dalton_settings
from src.dalton.tools.coverage_tools import build_coverage_tools
from src.dalton.tools.director_tools import build_director_tools

logger = logging.getLogger(__name__)


def build_dalton_tools(
    role: str,
    filesystem_backend: Any = None,
    *,
    enabled: bool | None = None,
    user_tier: str | None = None,
) -> list[BaseTool]:
    """Return role-appropriate Dalton tools to bind onto the main agent (`DALTON-HOOK(dalton-tools)`).

    - `chief_of_staff` (Home / Research Director):
      `dalton_coverage_board`, `dalton_record_plan`, `dalton_cross_unit_request` (coverage_units.enabled only), `dalton_agenda_note`, `dalton_watch_event`, `dalton_resolve_signals`, `dalton_schedule_automation`, `dalton_coverage_tracking`, `dalton_system_jobs`, `dalton_coverage`, `conversation_search` (internal/admin only), `check_language`, `ingest_company_wiki`, `dalton_check_done` (weekly-coverage-review, dalton-earnings-calendar only), `feishu_read`, `feishu_list_wiki`, `feishu_create_doc`, `feishu_append`, `feishu_grant_access`
    - `analyst` (Company workspace):
      `knowledge_update`, `knowledge_append_verdict`, `dalton_check_done`, `check_draft`, `check_language`, `publish_report`, `ingest_company_wiki`, `dalton_schedule_automation`, `dalton_coverage_tracking`, `dalton_system_jobs`, `conversation_search` (internal/admin only), `feishu_read`, `feishu_list_wiki`, `feishu_create_doc`, `feishu_append`, `feishu_grant_access`

    ``user_tier`` ("admin" | "internal" | "external") withholds
    ``conversation_search`` from external members, who would only reach its
    per-call refusal. None leaves the tool mounted (the refusal stays the
    second line of defense for builds without a resolved tier).

    ``prepare_transcription`` mounts only when ``is_transcription_enabled()``
    is true (``DALTON_TRANSCRIPTION_ENABLED`` on and storage configured); with
    the master switch off the agent never sees a tool it cannot run.
    """
    from src.dalton.services.transcriptions import is_transcription_enabled

    settings = get_dalton_settings()
    is_enabled = enabled if enabled is not None else (
        settings.enabled
        or settings.knowledge.enabled
        or settings.publish.enabled
        or settings.autonomy.enabled
        or settings.conversations.enabled
    )
    if not is_enabled:
        return []

    tools: list[BaseTool] = []

    # The team conversation archive is internal-only; an external member's
    # every call would be refused at run time, so the tool is not mounted for
    # them in the first place ("external" is also what a failed tier lookup
    # resolves to — fail closed on mounting).
    tier_mounts_conversation_search = user_tier is None or user_tier != "external"

    if settings.enabled:
        try:
            from src.dalton.tools.feishu_tools import create_feishu_tools

            tools.extend(create_feishu_tools(filesystem_backend=filesystem_backend))
        except Exception as e:
            logger.debug("[DALTON_TOOLS] feishu_tools not loaded: %s", e)

        # E3: the controlled bug-report inbox — internal only, never GitHub.
        try:
            from src.dalton.tools.issue_tools import create_issue_tools

            tools.extend(create_issue_tools())
        except Exception as e:
            logger.debug("[DALTON_TOOLS] issue_tools not loaded: %s", e)

    if role == "chief_of_staff":
        if settings.knowledge.enabled:
            try:
                from src.dalton.tools.knowledge_tools import create_knowledge_tools

                tools.extend(
                    create_knowledge_tools(filesystem_backend=filesystem_backend)
                )
            except Exception as e:
                logger.debug("[DALTON_TOOLS] knowledge_tools not loaded: %s", e)
        if settings.autonomy.enabled:
            tools.extend(build_director_tools(role=role, filesystem_backend=filesystem_backend))
            tools.extend(build_coverage_tools(role=role))
        if settings.conversations.enabled and tier_mounts_conversation_search:
            try:
                from src.dalton.tools.conversation_tools import create_conversation_tools

                tools.extend(create_conversation_tools())
            except Exception as e:
                logger.debug("[DALTON_TOOLS] conversation_tools not loaded: %s", e)
        if settings.publish.enabled:
            try:
                from src.dalton.tools.publish_tools import create_language_tools

                tools.extend(create_language_tools(filesystem_backend=filesystem_backend))
            except Exception as e:
                logger.debug("[DALTON_TOOLS] language_tools not loaded: %s", e)
        if settings.knowledge.enabled or settings.autonomy.enabled:
            try:
                # Home runs only the weekly review and earnings calendar: only
                # their contracts are checkable here (the role restriction lives in the tool).
                from src.dalton.tools.check_done_tools import create_check_done_tools

                tools.extend(
                    create_check_done_tools(
                        filesystem_backend=filesystem_backend, role="chief_of_staff"
                    )
                )
            except Exception as e:
                logger.debug("[DALTON_TOOLS] check_done_tools not loaded: %s", e)
        try:
            from src.dalton.tools.wiki_tools import build_ingest_company_wiki_tool

            tools.append(
                build_ingest_company_wiki_tool(
                    filesystem_backend=filesystem_backend,
                    allow_home_workspace=True,
                )
            )
        except Exception as e:
            logger.debug("[DALTON_TOOLS] wiki_tools not loaded: %s", e)
        # D1: the sales-notes ingest and the unified router. Internal only —
        # both refuse external members per call, and are mounted under the
        # same gate as the wiki ingest.
        try:
            from src.dalton.tools.ingest_tools import (
                build_ingest_research_tool,
                build_ingest_sales_note_tool,
            )

            tools.append(
                build_ingest_sales_note_tool(
                    filesystem_backend=filesystem_backend,
                    allow_home_workspace=True,
                )
            )
            tools.append(
                build_ingest_research_tool(
                    filesystem_backend=filesystem_backend,
                    allow_home_workspace=True,
                )
            )
        except Exception as e:
            logger.debug("[DALTON_TOOLS] ingest_tools not loaded: %s", e)
        if is_transcription_enabled():
            try:
                from src.dalton.tools.transcription_tools import (
                    build_prepare_transcription_tool,
                )

                tools.append(
                    build_prepare_transcription_tool(
                        filesystem_backend=filesystem_backend,
                        allow_home_workspace=True,
                    )
                )
            except Exception as e:
                logger.debug("[DALTON_TOOLS] transcription_tools not loaded: %s", e)
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

    if settings.conversations.enabled and tier_mounts_conversation_search:
        try:
            from src.dalton.tools.conversation_tools import create_conversation_tools

            tools.extend(create_conversation_tools())
        except Exception as e:
            logger.debug("[DALTON_TOOLS] conversation_tools not loaded: %s", e)

    try:
        from src.dalton.tools.wiki_tools import build_ingest_company_wiki_tool

        tools.append(
            build_ingest_company_wiki_tool(
                filesystem_backend=filesystem_backend,
                allow_home_workspace=False,
            )
        )
    except Exception as e:
        logger.debug("[DALTON_TOOLS] wiki_tools not loaded: %s", e)

    # D1: sales-notes ingest and the unified router (analyst workspaces).
    try:
        from src.dalton.tools.ingest_tools import (
            build_ingest_research_tool,
            build_ingest_sales_note_tool,
        )

        tools.append(
            build_ingest_sales_note_tool(
                filesystem_backend=filesystem_backend,
                allow_home_workspace=False,
            )
        )
        tools.append(
            build_ingest_research_tool(
                filesystem_backend=filesystem_backend,
                allow_home_workspace=False,
            )
        )
    except Exception as e:
        logger.debug("[DALTON_TOOLS] ingest_tools not loaded: %s", e)

    if is_transcription_enabled():
        try:
            from src.dalton.tools.transcription_tools import (
                build_prepare_transcription_tool,
            )

            tools.append(
                build_prepare_transcription_tool(
                    filesystem_backend=filesystem_backend,
                    allow_home_workspace=False,
                )
            )
        except Exception as e:
            logger.debug("[DALTON_TOOLS] transcription_tools not loaded: %s", e)

    return tools
