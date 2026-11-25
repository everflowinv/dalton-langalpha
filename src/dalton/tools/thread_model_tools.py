"""`switch_thread_model`: let the main agent switch the current thread's model
when explicitly asked by the user in a user-initiated turn.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Annotated, Any, Optional

from fastapi import HTTPException
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import InjectedState

from src.dalton.llm.providers import ProviderDisabledError, ensure_provider_enabled
from src.llms.llm import LLM as LLMFactory
from src.llms.preferences import custom_model, custom_model_names
from src.server.services.llm import user_models
from src.server.services.llm.claude_code import is_claude_code_model, rows_available
from src.server.services.llm.thread_model import switch_thread_model
from src.tools.wakeups.quiet import _last_boundary, _user_steer

logger = logging.getLogger(__name__)

_MAX_LISTED_CANDIDATES = 40


def _running_model(config: Any) -> str | None:
    """The model this turn was built with (``dalton_turn_model`` in configurable)."""
    cfg = (config or {}).get("configurable") or {}
    value = str(cfg.get("dalton_turn_model") or "").strip()
    return value or None


def _running_run_id(config: Any) -> str | None:
    """The current turn's run_id (``run_id`` in configurable)."""
    cfg = (config or {}).get("configurable") or {}
    value = str(cfg.get("run_id") or "").strip()
    return value or None


def _steer_belongs_to_turn(
    message: BaseMessage,
    run_id: str | None,
    *,
    after_boundary: bool,
) -> bool:
    """Whether a checkpointed user steering message belongs to the current turn."""
    if not _user_steer(message):
        return False
    kwargs = message.additional_kwargs or {}
    held = kwargs.get("held_jobs") or kwargs.get("held_reports")
    if isinstance(held, dict) and held.get("run_id"):
        return bool(run_id) and str(held["run_id"]) == run_id
    return after_boundary


def _user_steered(
    messages: Sequence[BaseMessage],
    config: RunnableConfig | None = None,
) -> bool:
    """True only when a user steering message belongs to the current turn.

    Never scans backward across the turn boundary into an earlier turn that may
    have stopped before emitting an ``AIMessage``. Pre-turn steers-ahead
    messages placed before the opener only count when ``held_jobs.run_id``
    matches the current turn's ``run_id``.
    """
    configurable = (config or {}).get("configurable") or {}
    run_id = str(configurable.get("run_id") or "").strip() or None
    start = _last_boundary(messages)
    if any(
        _steer_belongs_to_turn(m, run_id, after_boundary=True)
        for m in messages[start + 1 :]
    ):
        return True
    if run_id and start > 0:
        return any(
            _steer_belongs_to_turn(m, run_id, after_boundary=False)
            for m in messages[:start]
        )
    return False


def _extract_run_identity(
    config: RunnableConfig | None,
) -> tuple[str | None, str | None]:
    """Extract ``(user_id, thread_id)`` from ``RunnableConfig.configurable``."""
    cfg = (config or {}).get("configurable") or {}
    user_id = str(cfg.get("user_id") or "").strip() or None
    thread_id = str(cfg.get("thread_id") or "").strip() or None
    return user_id, thread_id


def _is_direct_user_turn(
    config: RunnableConfig | None,
    messages: Sequence[BaseMessage],
) -> bool:
    """Accept only turns initiated directly by the user (``origin == 'user'``)
    or turns that received a live user steer.

    Unlike ``empty_call_retry._user_sent_turn`` — which also treats
    ``origin == 'agent'`` (Chief-of-Staff to analyst handoff) as interactive so
    empty tool retries still apply — ``switch_thread_model`` changes persistent
    conversation settings and must reject ``origin == 'agent'``, automation,
    wakeup/report-back (``system``), unknown origin, and subagent (``task:*``)
    turns unless the user explicitly steered this turn.
    """
    cfg = config or {}
    configurable = cfg.get("configurable") or {}
    if str(configurable.get("checkpoint_ns") or "").split("|", 1)[0].startswith("task:"):
        return False
    retried_origin = configurable.get("retried_origin")
    if retried_origin is not None and retried_origin not in ("user", "turn"):
        return bool(messages) and _user_steered(messages, cfg)
    if (cfg.get("metadata") or {}).get("origin") == "user":
        return True
    return bool(messages) and _user_steered(messages, cfg)


def _model_provider_pair(
    model_key: str,
    *,
    pref: dict[str, Any] | None = None,
    mc: Any = None,
) -> tuple[str | None, str | None]:
    """Return ``(provider, parent_provider)`` for a catalog, custom model, or custom provider."""
    if pref:
        custom = custom_model(pref, model_key)
        if custom is not None:
            provider = custom.get("provider")
            parent_provider = custom.get("parent_provider")
            if (
                not parent_provider
                and isinstance(provider, str)
                and mc is not None
            ):
                pinfo = mc.get_provider_info(provider)
                if isinstance(pinfo, dict):
                    parent_provider = pinfo.get("parent_provider")
            return (
                str(provider) if isinstance(provider, str) else None,
                str(parent_provider) if isinstance(parent_provider, str) else None,
            )
        for cp in pref.get("custom_providers") or []:
            if isinstance(cp, dict) and cp.get("name") == model_key:
                parent_provider = cp.get("parent_provider")
                return (
                    model_key,
                    str(parent_provider) if isinstance(parent_provider, str) else None,
                )
    if mc is not None:
        info = mc.get_model_config(model_key)
        if isinstance(info, dict):
            provider = info.get("provider")
            parent_provider = None
            if isinstance(provider, str):
                pinfo = mc.get_provider_info(provider)
                if isinstance(pinfo, dict):
                    parent_provider = pinfo.get("parent_provider")
            return (
                str(provider) if isinstance(provider, str) else None,
                str(parent_provider) if isinstance(parent_provider, str) else None,
            )
    return None, None


def _is_runtime_enabled(
    model_key: str,
    *,
    pref: dict[str, Any] | None = None,
    mc: Any = None,
) -> bool:
    """Whether ``model_key`` and its provider are enabled on this deployment."""
    if is_claude_code_model(model_key) and not rows_available():
        return False
    catalog = mc if mc is not None else LLMFactory.get_model_config()
    provider, parent_provider = _model_provider_pair(model_key, pref=pref, mc=catalog)
    if provider:
        try:
            ensure_provider_enabled(provider, parent_provider=parent_provider)
        except ProviderDisabledError:
            return False
    return True


def _selectable_entries(pref: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Catalog + custom entries whose runtime and provider are enabled in this deployment."""
    mc = LLMFactory.get_model_config()
    return [
        (key, dn)
        for key, dn in _catalog_entries(pref)
        if _is_runtime_enabled(key, pref=pref, mc=mc)
    ]


def _custom_provider_names(pref: dict[str, Any]) -> set[str]:
    providers = pref.get("custom_providers")
    if not isinstance(providers, list):
        return set()
    return {
        cp["name"]
        for cp in providers
        if isinstance(cp, dict) and isinstance(cp.get("name"), str) and cp["name"]
    }


def _catalog_entries(pref: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Return ``[(key, display_name)]`` for selectable catalog + custom models."""
    mc = LLMFactory.get_model_config()
    entries: dict[str, str | None] = {}

    listed = mc.listed_models()
    source_items = (
        listed
        if listed
        else [
            (k, v)
            for k, v in (mc.llm_config or {}).items()
            if isinstance(v, dict)
        ]
    )
    for key, info in source_items:
        dn = info.get("display_name") if isinstance(info, dict) else None
        entries[key] = str(dn) if isinstance(dn, str) and dn.strip() else None

    for cm in pref.get("custom_models") or []:
        if not isinstance(cm, dict) or not isinstance(cm.get("name"), str):
            continue
        name = cm["name"].strip()
        if not name:
            continue
        dn = cm.get("display_name")
        entries[name] = str(dn) if isinstance(dn, str) and dn.strip() else None

    return list(entries.items())


def _display_label(pref: dict[str, Any], key: str) -> str:
    """Human-readable label for a resolved model key."""
    custom = custom_model(pref, key)
    entry = custom if custom is not None else (LLMFactory.get_model_config().get_model_config(key) or {})
    dn = entry.get("display_name") if isinstance(entry, dict) else None
    if isinstance(dn, str) and dn.strip() and dn.strip() != key:
        return f"{dn.strip()} ({key})"
    return key


def _format_candidate(key: str, display_name: str | None) -> str:
    dn = display_name.strip() if isinstance(display_name, str) else ""
    return f"{dn} ({key})" if dn and dn != key else key


def _format_candidates(items: Sequence[tuple[str, str | None]]) -> str:
    shown = [_format_candidate(k, dn) for k, dn in items[:_MAX_LISTED_CANDIDATES]]
    suffix = f" 等共 {len(items)} 个" if len(items) > _MAX_LISTED_CANDIDATES else ""
    return ", ".join(shown) + suffix


def _resolve_model_target(
    raw_model: str,
    pref: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """Resolve a model key or case-insensitive exact ``display_name`` to
    ``(model_key, display_label, error_message)``.
    """
    candidate = str(raw_model or "").strip()
    entries = _catalog_entries(pref)
    selectable = _selectable_entries(pref)
    if not candidate:
        return (
            None,
            None,
            f"模型名称不能为空，请传入模型 ID 或显示名。可选模型：{_format_candidates(selectable)}。",
        )

    mc = LLMFactory.get_model_config()
    if (
        candidate in custom_model_names(pref)
        or candidate in _custom_provider_names(pref)
        or mc.get_model_config(candidate) is not None
    ):
        return candidate, _display_label(pref, candidate), None

    needle = candidate.casefold()
    exact_display_matches = [
        (key, dn)
        for key, dn in selectable
        if dn is not None and dn.strip().casefold() == needle
    ]
    if not exact_display_matches:
        exact_display_matches = [
            (key, dn)
            for key, dn in entries
            if dn is not None and dn.strip().casefold() == needle
        ]
    if len(exact_display_matches) == 1:
        key, _ = exact_display_matches[0]
        return key, _display_label(pref, key), None
    if len(exact_display_matches) > 1:
        return (
            None,
            None,
            f"模型名称 '{candidate}' 匹配到多个模型，请改用括号内的模型 ID：{_format_candidates(exact_display_matches)}。",
        )

    close_matches = [
        (key, dn)
        for key, dn in selectable
        if needle in key.casefold() or (dn is not None and needle in dn.casefold())
    ]
    pool = close_matches if close_matches else selectable
    return (
        None,
        None,
        f"未找到模型 '{candidate}'，请改用可选模型中的模型 ID 或显示名。可选模型：{_format_candidates(pool)}。",
    )


def create_thread_model_tools() -> list[BaseTool]:
    """Build `[switch_thread_model]`, mounted for main agents only."""

    @tool("switch_thread_model")
    async def _switch_thread_model(
        model: str,
        config: RunnableConfig,
        state: Annotated[Optional[dict[str, Any]], InjectedState] = None,
    ) -> str:
        """Switch only the current thread's model when the user explicitly asks to change it.
        Do not call this on automated, scheduled, wakeup, report-back, or delegated turns unless the user explicitly asked in a steering message on this turn, and do not use this when only a subagent or delegated task needs a different model (pass the `model` parameter on that call instead).

        Args:
            model: Available model ID or the exact display name of a single model entry (case-insensitive). Built-in Antigravity models use effort-suffixed IDs (e.g. `gemini-4-neon-high-oauth`); if the user gives only a series name (such as `Gemini 4 Neon`) without an effort level, confirm the effort level first.

        Returns:
            Short status confirmation with the selected model and when it takes effect, or an error listing available models.

        Tell the user the switch applies from their next message onward and this reply still finishes on the current model.
        """
        messages = list((state or {}).get("messages") or [])
        if not _is_direct_user_turn(config, messages):
            return (
                "拒绝切换模型：只能在用户本轮明确要求时切换当前对话的模型"
                "（自动任务、定时唤醒、任务汇报、助手派发和子代理轮次不可调用）。"
            )

        user_id, thread_id = _extract_run_identity(config)
        if not user_id or not thread_id:
            return "切换模型失败：缺少用户标识或对话标识，无法确定目标对话。"

        pref = await user_models.get_model_preference(user_id)
        resolved_key, display_label, err = _resolve_model_target(model, pref)
        if err is not None or resolved_key is None or display_label is None:
            return err or f"未找到模型 '{model}'。"

        if not _is_runtime_enabled(resolved_key, pref=pref):
            candidates = _format_candidates(_selectable_entries(pref))
            return f"切换模型失败：模型 {display_label} 在当前服务中未启用。可选模型：{candidates}。"

        try:
            running_run_id = _running_run_id(config)
            result = await switch_thread_model(
                user_id,
                thread_id,
                resolved_key,
                defer_handoff=True,
                model_pref=pref,
                short_circuit_default=True,
                running_model=_running_model(config),
                **({"running_run_id": running_run_id} if running_run_id else {}),
            )
        except HTTPException as exc:
            detail = exc.detail
            msg = detail.get("message") if isinstance(detail, dict) else str(detail)
            if isinstance(detail, dict) and detail.get("type") == "model_unavailable":
                candidates = _format_candidates(_selectable_entries(pref))
                return f"切换模型失败：{msg} 可选模型：{candidates}。"
            return f"切换模型失败：{msg}"

        if not result.changed:
            return f"当前对话已在使用模型 {display_label}，无需切换。"

        return f"已切换到 {display_label}，从用户的下一条消息起生效；本轮仍由当前模型完成。"

    return [_switch_thread_model]


_DEFAULT_TOOLS = create_thread_model_tools()
switch_thread_model_tool = _DEFAULT_TOOLS[0]
