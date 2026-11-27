"""Unified declarative Dalton tool registry and builder (`build_dalton_tools`) for main agents (PK.4, PK.11, PK.15, PK.19)."""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.tools import BaseTool

from src.dalton.capabilities import (
    CORE_CAPABILITY,
    CapabilityId,
    CapabilityRequirement,
    effective_capabilities,
    is_capability_requirement_satisfied,
)
from src.dalton.settings import get_dalton_settings
from src.dalton.tools.coverage_tools import build_coverage_tools
from src.dalton.tools.director_tools import build_director_tools

logger = logging.getLogger(__name__)

ToolRole = Literal["analyst", "chief_of_staff"]
ToolMinTier = Literal["any", "non_external", "internal"]
ToolKwargMode = Literal[
    "none",
    "filesystem",
    "allow_home",
    "fs_and_allow_home",
    "role_and_fs",
    "role",
    "fs_and_cos_role",
]

_ALL_ROLES: frozenset[str] = frozenset({"analyst", "chief_of_staff"})
_COS_ONLY: frozenset[str] = frozenset({"chief_of_staff"})
_ANALYST_ONLY: frozenset[str] = frozenset({"analyst"})


@dataclass(frozen=True)
class DaltonToolRegistration:
    """Declarative registration entry for one Dalton tool factory."""

    id: str
    module: str
    builder_name: str
    tool_names: tuple[str, ...]
    roles: frozenset[str]
    min_tier: ToolMinTier
    capability: CapabilityRequirement
    description: str
    any_capabilities: tuple[CapabilityId, ...] = ()
    kwarg_mode: ToolKwargMode = "none"
    single_tool: bool = False
    setting_gate: str | None = None
    swallow_errors: bool = True


# Canonical ordered registry of every Dalton tool factory under `src/dalton/tools/`.
# Order matches the existing Everflow mounting order so Everflow tool lists stay
# byte-identical. Every tool exported from `src/dalton/tools/*.py` must be listed
# here with either `CORE_CAPABILITY` (None) or a specific `CapabilityId`.
DALTON_TOOL_REGISTRY: tuple[DaltonToolRegistration, ...] = (
    # --- Internal/admin tools mounted for both roles ---
    DaltonToolRegistration(
        id="notify_tools",
        module="src.dalton.tools.notify_tools",
        builder_name="create_notify_tools",
        tool_names=("dalton_notify_settings",),
        roles=_ALL_ROLES,
        min_tier="non_external",
        capability=CapabilityId.CHANNEL_GATEWAY,
        description="飞书/Discord 个人与群推送通知设置",
    ),
    DaltonToolRegistration(
        id="automation_report_tools",
        module="src.dalton.tools.automation_report_tools",
        builder_name="create_automation_report_tools",
        tool_names=("automation_report",),
        roles=_ALL_ROLES,
        min_tier="non_external",
        capability=CapabilityId.FUND_AUTOMATIONS,
        description="内部定时简报卡片/文本提交与状态查询",
    ),
    DaltonToolRegistration(
        id="report_delivery_tools",
        module="src.dalton.tools.report_delivery_tools",
        builder_name="create_report_delivery_tools",
        tool_names=("dalton_report_delivery",),
        roles=_ALL_ROLES,
        min_tier="non_external",
        capability=CapabilityId.FUND_AUTOMATIONS,
        description="内部定时简报发送对象（report_profile）自助配置",
    ),
    DaltonToolRegistration(
        id="market_digest_tools",
        module="src.dalton.tools.market_digest_tools",
        builder_name="create_market_digest_tools",
        tool_names=("market_digest_inputs",),
        roles=_ALL_ROLES,
        min_tier="non_external",
        capability=CapabilityId.FUND_AUTOMATIONS,
        description="内部市场摘要批次输入读取",
    ),
    DaltonToolRegistration(
        id="market_digest_output_tools",
        module="src.dalton.tools.market_digest_output_tools",
        builder_name="create_market_digest_output_tools",
        tool_names=("market_digest_output",),
        roles=_ALL_ROLES,
        min_tier="non_external",
        capability=CapabilityId.FUND_AUTOMATIONS,
        description="内部市场摘要输出提交",
    ),
    DaltonToolRegistration(
        id="ai_finance_inputs_tools",
        module="src.dalton.tools.ai_finance_inputs_tools",
        builder_name="create_ai_finance_inputs_tools",
        tool_names=("ai_finance_prepare", "ai_finance_read"),
        roles=_ALL_ROLES,
        min_tier="non_external",
        capability=CapabilityId.FUND_AUTOMATIONS,
        description="内部 AI Finance 日报数据准备与读取",
    ),
    DaltonToolRegistration(
        id="ai_finance_signals_tools",
        module="src.dalton.tools.ai_finance_signals_tools",
        builder_name="create_ai_finance_signals_tools",
        tool_names=("ai_finance_signals",),
        roles=_ALL_ROLES,
        min_tier="non_external",
        capability=CapabilityId.FUND_AUTOMATIONS,
        description="内部 AI Finance 信号管理",
    ),
    DaltonToolRegistration(
        id="ai_finance_report_tools",
        module="src.dalton.tools.ai_finance_report_tools",
        builder_name="create_ai_finance_report_tools",
        tool_names=("ai_finance_report",),
        roles=_ALL_ROLES,
        min_tier="non_external",
        capability=CapabilityId.FUND_AUTOMATIONS,
        description="内部 AI Finance 日报提交",
    ),
    DaltonToolRegistration(
        id="podcast_weekly_tools",
        module="src.dalton.tools.podcast_weekly_tools",
        builder_name="create_podcast_weekly_tools",
        tool_names=("podcast_weekly_read", "podcast_weekly_write"),
        roles=_ALL_ROLES,
        min_tier="non_external",
        capability=CapabilityId.FUND_AUTOMATIONS,
        description="内部播客周报读写",
        kwarg_mode="filesystem",
    ),
    # --- Shared Dalton tools mounted for both roles ---
    DaltonToolRegistration(
        id="feishu_tools",
        module="src.dalton.tools.feishu_tools",
        builder_name="create_feishu_tools",
        tool_names=(
            "feishu_read",
            "feishu_list_wiki",
            "feishu_create_doc",
            "feishu_append",
            "feishu_grant_access",
        ),
        roles=_ALL_ROLES,
        min_tier="any",
        capability=CapabilityId.FEISHU_DOCS,
        description="飞书文档与知识空间读写",
        kwarg_mode="filesystem",
        setting_gate="dalton_enabled",
    ),
    DaltonToolRegistration(
        id="issue_tools",
        module="src.dalton.tools.issue_tools",
        builder_name="create_issue_tools",
        tool_names=("report_issue",),
        roles=_ALL_ROLES,
        min_tier="any",
        capability=CapabilityId.PROBLEM_REPORTS,
        description="内部运行问题工单上报工具（report_issue）",
        setting_gate="dalton_enabled",
    ),
    DaltonToolRegistration(
        id="thread_model_tools",
        module="src.dalton.tools.thread_model_tools",
        builder_name="create_thread_model_tools",
        tool_names=("switch_thread_model",),
        roles=_ALL_ROLES,
        min_tier="any",
        capability=CORE_CAPABILITY,
        description="核心：当前线程模型切换",
    ),
    DaltonToolRegistration(
        id="monitor_tools",
        module="src.dalton.tools.monitor_tools",
        builder_name="create_monitor_tools",
        tool_names=("dalton_monitor",),
        roles=_ALL_ROLES,
        min_tier="any",
        capability=CORE_CAPABILITY,
        description="核心：工作区交互式监控面板发布与更新",
        kwarg_mode="allow_home",
        setting_gate="artifact_publishing",
    ),
    DaltonToolRegistration(
        id="calendar_tools",
        module="src.dalton.tools.calendar_tools",
        builder_name="create_calendar_tools",
        tool_names=("dalton_calendar",),
        roles=_ALL_ROLES,
        min_tier="any",
        capability=CORE_CAPABILITY,
        any_capabilities=(
            CapabilityId.GOOGLE_CALENDAR,
            CapabilityId.FEISHU_CALENDAR,
        ),
        description="个人日历读写（Google 日历与飞书日历，按已启用能力门控）",
        setting_gate="dalton_enabled",
    ),
    # --- Chief of Staff role tools ---
    DaltonToolRegistration(
        id="knowledge_tools_cos",
        module="src.dalton.tools.knowledge_tools",
        builder_name="create_knowledge_tools",
        tool_names=("knowledge_update", "knowledge_append_verdict", "knowledge_read"),
        roles=_COS_ONLY,
        min_tier="any",
        capability=CORE_CAPABILITY,
        description="核心：公司与行业投研知识层读写",
        kwarg_mode="role_and_fs",
        setting_gate="knowledge",
    ),
    DaltonToolRegistration(
        id="director_tools_cos",
        module="src.dalton.tools.director_tools",
        builder_name="build_director_tools",
        tool_names=(
            "dalton_coverage_board",
            "dalton_record_plan",
            "dalton_cross_unit_request",
            "dalton_coverage_unit",
            "dalton_agenda_note",
            "dalton_watch_event",
            "dalton_resolve_signals",
            "dalton_publish_earnings_calendar",
            "dalton_schedule_automation",
            "dalton_coverage_tracking",
            "dalton_system_jobs",
        ),
        roles=_COS_ONLY,
        min_tier="any",
        capability=CORE_CAPABILITY,
        description="核心：研究总监看板、覆盖单元、追踪与系统任务工具",
        kwarg_mode="role_and_fs",
        setting_gate="autonomy",
        swallow_errors=False,
    ),
    DaltonToolRegistration(
        id="coverage_tools_cos",
        module="src.dalton.tools.coverage_tools",
        builder_name="build_coverage_tools",
        tool_names=("dalton_coverage",),
        roles=_COS_ONLY,
        min_tier="any",
        capability=CORE_CAPABILITY,
        description="核心：用户覆盖标的名单管理",
        kwarg_mode="role",
        setting_gate="autonomy",
        swallow_errors=False,
    ),
    DaltonToolRegistration(
        id="conversation_tools_cos",
        module="src.dalton.tools.conversation_tools",
        builder_name="create_conversation_tools",
        tool_names=("conversation_search",),
        roles=_COS_ONLY,
        min_tier="internal",
        capability=CapabilityId.CONVERSATION_ARCHIVE,
        description="历史团队对话归档检索",
        kwarg_mode="role",
    ),
    DaltonToolRegistration(
        id="language_tools_cos",
        module="src.dalton.tools.publish_tools",
        builder_name="create_language_tools",
        tool_names=("check_language",),
        roles=_COS_ONLY,
        min_tier="any",
        capability=CORE_CAPABILITY,
        description="核心：研究文稿语言审校",
        kwarg_mode="filesystem",
        setting_gate="publish",
    ),
    DaltonToolRegistration(
        id="check_done_tools_cos",
        module="src.dalton.tools.check_done_tools",
        builder_name="create_check_done_tools",
        tool_names=("dalton_check_done",),
        roles=_COS_ONLY,
        min_tier="any",
        capability=CORE_CAPABILITY,
        description="核心：研究总监工作流闭环检查",
        kwarg_mode="fs_and_cos_role",
        setting_gate="knowledge_or_autonomy",
    ),
    # --- Analyst role tools ---
    DaltonToolRegistration(
        id="colleague_workspace_analyst",
        module="src.dalton.tools.colleague_workspace_tools",
        builder_name="create_colleague_workspace_tools",
        tool_names=("colleague_workspace",),
        roles=_ANALYST_ONLY,
        min_tier="any",
        capability=CapabilityId.COLLEAGUE_SHARING,
        description="工作区分析师跨工作区读取与向当前工作区服务端复制",
    ),
    DaltonToolRegistration(
        id="knowledge_tools_analyst",
        module="src.dalton.tools.knowledge_tools",
        builder_name="create_knowledge_tools",
        tool_names=("knowledge_update", "knowledge_append_verdict", "knowledge_read"),
        roles=_ANALYST_ONLY,
        min_tier="any",
        capability=CORE_CAPABILITY,
        description="核心：公司与行业投研知识层读写",
        kwarg_mode="role_and_fs",
        setting_gate="knowledge",
    ),
    DaltonToolRegistration(
        id="check_done_tools_analyst",
        module="src.dalton.tools.check_done_tools",
        builder_name="create_check_done_tools",
        tool_names=("dalton_check_done",),
        roles=_ANALYST_ONLY,
        min_tier="any",
        capability=CORE_CAPABILITY,
        description="核心：分析师工作流闭环检查",
        kwarg_mode="filesystem",
        setting_gate="knowledge",
    ),
    DaltonToolRegistration(
        id="publish_tools_analyst",
        module="src.dalton.tools.publish_tools",
        builder_name="create_publish_tools",
        tool_names=("check_draft", "check_language", "publish_report"),
        roles=_ANALYST_ONLY,
        min_tier="any",
        capability=CORE_CAPABILITY,
        description="核心：研报数字核验、语言审校与正式发布",
        kwarg_mode="filesystem",
        setting_gate="publish",
    ),
    DaltonToolRegistration(
        id="director_tools_analyst",
        module="src.dalton.tools.director_tools",
        builder_name="build_director_tools",
        tool_names=(
            "dalton_schedule_automation",
            "dalton_coverage_tracking",
            "dalton_system_jobs",
        ),
        roles=_ANALYST_ONLY,
        min_tier="any",
        capability=CORE_CAPABILITY,
        description="核心：分析师自动化调度、覆盖追踪与系统任务工具",
        kwarg_mode="role_and_fs",
        setting_gate="autonomy",
        swallow_errors=False,
    ),
    DaltonToolRegistration(
        id="conversation_tools_analyst",
        module="src.dalton.tools.conversation_tools",
        builder_name="create_conversation_tools",
        tool_names=("conversation_search",),
        roles=_ANALYST_ONLY,
        min_tier="internal",
        capability=CapabilityId.CONVERSATION_ARCHIVE,
        description="历史团队对话归档检索",
        kwarg_mode="role",
    ),
    # --- Bridge-backed ingest & transcription tools (both roles) ---
    DaltonToolRegistration(
        id="wiki_tools",
        module="src.dalton.tools.wiki_tools",
        builder_name="build_ingest_company_wiki_tool",
        tool_names=("ingest_company_wiki",),
        roles=_ALL_ROLES,
        min_tier="any",
        capability=CapabilityId.COMPANY_WIKI,
        description="内部公司资料库（Wiki）入库",
        kwarg_mode="fs_and_allow_home",
        single_tool=True,
    ),
    DaltonToolRegistration(
        id="ingest_sales_note_tool",
        module="src.dalton.tools.ingest_tools",
        builder_name="build_ingest_sales_note_tool",
        tool_names=("ingest_sales_note",),
        roles=_ALL_ROLES,
        min_tier="any",
        capability=CapabilityId.SALES_NOTES,
        description="销售笔记入库",
        kwarg_mode="fs_and_allow_home",
        single_tool=True,
    ),
    DaltonToolRegistration(
        id="ingest_research_tool",
        module="src.dalton.tools.ingest_tools",
        builder_name="build_ingest_research_tool",
        tool_names=("ingest_research",),
        roles=_ALL_ROLES,
        min_tier="any",
        capability=(CapabilityId.COMPANY_WIKI, CapabilityId.SALES_NOTES),
        description="研究资料统一入库路由（Wiki / 销售笔记）",
        kwarg_mode="fs_and_allow_home",
        single_tool=True,
    ),
    DaltonToolRegistration(
        id="transcription_tools",
        module="src.dalton.tools.transcription_tools",
        builder_name="build_prepare_transcription_tool",
        tool_names=("prepare_transcription",),
        roles=_ALL_ROLES,
        min_tier="any",
        capability=CapabilityId.TRANSCRIPTION,
        description="录音音频转写任务准备",
        kwarg_mode="fs_and_allow_home",
        single_tool=True,
        setting_gate="transcription",
    ),
)


def _passes_setting_gate(
    gate: str | None,
    *,
    settings: Any,
    artifact_publishing_on: bool,
    transcription_on: bool,
) -> bool:
    if gate is None:
        return True
    if gate == "dalton_enabled":
        return bool(settings.enabled)
    if gate == "artifact_publishing":
        return bool(settings.enabled and artifact_publishing_on)
    if gate == "knowledge":
        return bool(settings.knowledge.enabled)
    if gate == "autonomy":
        return bool(settings.autonomy.enabled)
    if gate == "conversations":
        return bool(settings.conversations.enabled)
    if gate == "publish":
        return bool(settings.publish.enabled)
    if gate == "knowledge_or_autonomy":
        return bool(settings.knowledge.enabled or settings.autonomy.enabled)
    if gate == "transcription":
        return bool(transcription_on)
    raise ValueError(f"Unknown Dalton tool setting_gate: {gate!r}")


def _passes_tier_gate(
    min_tier: ToolMinTier,
    *,
    tier_mounts_internal_tools: bool,
    tier_mounts_conversation_search: bool,
) -> bool:
    if min_tier == "any":
        return True
    if min_tier == "non_external":
        return tier_mounts_internal_tools
    if min_tier == "internal":
        return tier_mounts_conversation_search
    raise ValueError(f"Unknown Dalton tool min_tier: {min_tier!r}")


def _resolve_builder(entry: DaltonToolRegistration) -> Any:
    if entry.builder_name == "build_director_tools" and entry.module == "src.dalton.tools.director_tools":
        return build_director_tools
    if entry.builder_name == "build_coverage_tools" and entry.module == "src.dalton.tools.coverage_tools":
        return build_coverage_tools
    mod = importlib.import_module(entry.module)
    return getattr(mod, entry.builder_name)


def _invoke_tool_builder(
    entry: DaltonToolRegistration,
    *,
    role: str,
    filesystem_backend: Any,
) -> list[BaseTool]:
    builder = _resolve_builder(entry)
    allow_home = role == "chief_of_staff"

    if entry.kwarg_mode == "none":
        built = builder()
    elif entry.kwarg_mode == "filesystem":
        built = builder(filesystem_backend=filesystem_backend)
    elif entry.kwarg_mode == "allow_home":
        built = builder(allow_home_workspace=allow_home)
    elif entry.kwarg_mode == "fs_and_allow_home":
        built = builder(
            filesystem_backend=filesystem_backend,
            allow_home_workspace=allow_home,
        )
    elif entry.kwarg_mode == "role_and_fs":
        built = builder(role=role, filesystem_backend=filesystem_backend)
    elif entry.kwarg_mode == "role":
        built = builder(role=role)
    elif entry.kwarg_mode == "fs_and_cos_role":
        built = builder(filesystem_backend=filesystem_backend, role="chief_of_staff")
    else:
        raise ValueError(f"Unknown kwarg_mode: {entry.kwarg_mode!r}")

    if entry.single_tool:
        return [built]
    return list(built)


def build_dalton_tools(
    role: str,
    filesystem_backend: Any = None,
    *,
    enabled: bool | None = None,
    user_tier: str | None = None,
    team_memory_enabled: bool = False,
    allow_personal_watchlist: bool = True,
) -> list[BaseTool]:
    """Return role- and capability-appropriate Dalton tools for the main agent (`DALTON-HOOK(dalton-tools)`).

    Tools are declared in :data:`DALTON_TOOL_REGISTRY` with their factory,
    allowed roles, minimum caller tier, and required :class:`CapabilityId`
    (or ``None`` for core-universal tools). Tools whose capability is disabled
    in the active deployment are never mounted.

    ``team_memory_enabled`` is kept for signature compatibility; top-level tool
    mounting follows deployment/tier availability only so switching the user
    toggle never changes the top-level tool list.
    """
    _ = team_memory_enabled
    from src.dalton.access_tiers import is_internal_tier
    from src.dalton.services.transcriptions import is_transcription_enabled
    from src.dalton.settings import is_artifact_publishing_enabled

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

    active_role = "chief_of_staff" if role == "chief_of_staff" else "analyst"
    active_caps = effective_capabilities(
        dalton_enabled=True,
        conversations_enabled=bool(settings.conversations.enabled),
        data_jobs_enabled=bool(settings.data_jobs.enabled),
    )

    # DALTON-HOOK(team-memory-toggle): mount conversation_search from deployment/tier availability only (not personal toggle)
    tier_mounts_internal_tools = user_tier is None or user_tier != "external"
    tier_mounts_conversation_search = user_tier is None or is_internal_tier(user_tier)
    artifact_publishing_on = is_artifact_publishing_enabled()
    transcription_on = is_transcription_enabled()

    tools: list[BaseTool] = []
    for entry in DALTON_TOOL_REGISTRY:
        if active_role not in entry.roles:
            continue
        if entry.id == "coverage_tools_cos" and not allow_personal_watchlist:
            continue
        if not _passes_tier_gate(
            entry.min_tier,
            tier_mounts_internal_tools=tier_mounts_internal_tools,
            tier_mounts_conversation_search=tier_mounts_conversation_search,
        ):
            continue
        if not is_capability_requirement_satisfied(entry.capability, active_caps):
            continue
        if entry.any_capabilities and not any(
            cap in active_caps for cap in entry.any_capabilities
        ):
            continue
        if not _passes_setting_gate(
            entry.setting_gate,
            settings=settings,
            artifact_publishing_on=artifact_publishing_on,
            transcription_on=transcription_on,
        ):
            continue
        if entry.swallow_errors:
            try:
                tools.extend(
                    _invoke_tool_builder(
                        entry,
                        role=role,
                        filesystem_backend=filesystem_backend,
                    )
                )
            except Exception as e:
                logger.debug("[DALTON_TOOLS] %s not loaded: %s", entry.id, e)
        else:
            tools.extend(
                _invoke_tool_builder(
                    entry,
                    role=role,
                    filesystem_backend=filesystem_backend,
                )
            )

    return tools


__all__ = [
    "DALTON_TOOL_REGISTRY",
    "DaltonToolRegistration",
    "build_coverage_tools",
    "build_dalton_tools",
    "build_director_tools",
]
