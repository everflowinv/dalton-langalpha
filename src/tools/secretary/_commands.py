"""What the hand-off tool answers with, and the checks it runs before acting.

Every answer is a ``Command`` carrying one ``ToolMessage``, and a hand-off
pauses on ``interrupt()`` for the user's approval, the same pattern the
onboarding tools use.
"""

import json
import logging
from collections.abc import Mapping  # DALTON-HOOK(computer-cli-tokens-off)
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import Command, interrupt

try:
    from langchain.tools import InjectedToolCallId
except ImportError:
    from langchain_core.tools import InjectedToolCallId

logger = logging.getLogger(__name__)

# DALTON-HOOK(computer-cli-tokens-off): retain server-side CoS command helpers.
__all__ = [
    "HANDOFF_TOOL_NAME",
    "InjectedToolCallId",
    "decline_command",
    "decline_handoff_command",
    "decline_routing_command",
    "error_command",
    "hitl_confirm",
    "puts_card",
    "success_command",
    "verify_thread_owner",
    "verify_workspace_owner",
]

#: The one Chief of Staff tool left, and the one that puts a card to the user.
HANDOFF_TOOL_NAME = "delegate_to_analyst"


def hitl_confirm(
    action_type: str, payload: dict[str, Any]
) -> tuple[bool, dict]:
    """Pause the graph for user confirmation via interrupt().

    Args:
        action_type: The action type string (e.g. "create_workspace")
        payload: Additional data to include in the action request

    Returns:
        Tuple of (approved, response_dict)
    """
    response = interrupt(
        {"action_requests": [{"type": action_type, **payload}]}
    )

    approved = False
    # DALTON-HOOK(cos-settings-tools): a server-stamped scope_changed resume never approves execution
    if isinstance(response, dict) and not response.get("scope_changed"):
        decisions = response.get("decisions", [])
        if decisions and decisions[0].get("type") == "approve":
            approved = True

    return approved, response if isinstance(response, dict) else {}


# --- DALTON-HOOK(computer-cli-tokens-off): begin ---
#: The tools that put a card to the user through ``hitl_confirm``, by the
#: actions that do (None for every action).
_CARDS: dict[str, frozenset[str] | None] = {
    "delegate_to_analyst": None,
    # DALTON-HOOK(cos-settings-tools): register archive confirmation card
    "manage_workspaces": frozenset({"create", "delete", "archive"}),
    "manage_threads": frozenset({"delete"}),
    # DALTON-HOOK(watchlist): dalton_coverage mutating actions may pause on hitl_confirm
    "dalton_coverage": frozenset(
        {
            "add",
            "set_tier",
            "remove",
            "add_group",
            "remove_group",
            "set_tier_policy",
            "reset_tier_policy",
        }
    ),
}


def puts_card(name: str, args: Mapping[str, Any]) -> bool:
    """Whether a call may stop for the user's answer on a card."""
    if name not in _CARDS:
        return False
    actions, action = _CARDS[name], args.get("action")
    return actions is None or (isinstance(action, str) and action in actions)


def decline_command(message: str, tool_call_id: str) -> Command:
    """Return a Command for a declined HITL action."""
    return Command(
        update={
            "messages": [
                ToolMessage(content=message, tool_call_id=tool_call_id),
            ],
        }
    )


def decline_routing_command(
    declined: str, response: dict, tool_call_id: str
) -> Command:
    """Decline a hand-off, or the workspace for one, without freeing the work.

    A bare decline read to the model as leave to do the work itself, though
    the user declined where it would go, not that it should be done here.
    """
    from src.server.models.chat import rejection_feedback

    decisions = response.get("decisions") or [{}]
    feedback = rejection_feedback(decisions[0].get("message"))
    said = f', saying: "{feedback}"' if feedback else ""
    return decline_command(
        f"User declined {declined}{said}. Unless they said to do the work "
        "here, do not do it yourself instead: ask them where they want it.",
        tool_call_id,
    )
# --- DALTON-HOOK(computer-cli-tokens-off): end ---


def decline_handoff_command(response: dict, tool_call_id: str) -> Command:
    """Decline a hand-off without freeing the work.

    A bare decline read to the model as leave to do the work itself, though
    the user declined where it would go, not that it should be done here.
    """
    from src.server.models.chat import rejection_feedback

    decisions = response.get("decisions") or [{}]
    feedback = rejection_feedback(decisions[0].get("message"))
    said = f', saying: "{feedback}"' if feedback else ""
    message = (
        f"User declined the hand-off{said}. Unless they said to do the work "
        "here, do not do it yourself instead: ask them where they want it."
    )
    return Command(
        update={"messages": [ToolMessage(content=message, tool_call_id=tool_call_id)]}
    )


def success_command(
    data: dict[str, Any], tool_call_id: str, artifact: dict[str, Any] | None = None
) -> Command:
    """Return a Command with JSON-serialized success data."""
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=json.dumps(data),
                    tool_call_id=tool_call_id,
                    artifact=artifact,
                ),
            ],
        }
    )


def error_command(
    error: str,
    tool_call_id: str,
    *,
    # DALTON-HOOK(cos-settings-tools): allow marking ToolMessage.status (e.g. "error" for scope_changed)
    message_status: str | None = None,
    **fields: Any,
) -> Command:
    """Return a Command with a JSON error response, plus any ``fields`` that
    say what the failed action was."""
    msg_kwargs: dict[str, Any] = {}
    if message_status is not None:
        msg_kwargs["status"] = message_status
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=json.dumps({"success": False, "error": error, **fields}),
                    tool_call_id=tool_call_id,
                    **msg_kwargs,
                ),
            ],
        }
    )


# DALTON-HOOK(computer-cli-tokens-off): retained for manage_workspaces / manage_threads.
async def verify_workspace_owner(
    workspace_id: str,
    user_id: str,
    tool_call_id: str,
    *,
    need: str = "read",
    owner_only: bool = False,
) -> Command | None:
    """Return error Command if user cannot access workspace, else None."""
    from src.server.database.workspace import get_workspace

    ws = await get_workspace(workspace_id)
    if not ws:
        return error_command("workspace not found", tool_call_id)
    if str(ws.get("user_id")) == user_id:
        return None
    # --- DALTON-HOOK(workspace-sharing-settings): begin ---
    # DALTON-HOOK(colleague-access): gate colleague workspace access on is_dalton_sharing_auth_enabled() and refuse reserved Home/stand-in rows.
    from src.server.database.home_workspace import is_reserved_row

    if is_reserved_row(ws) or bool(ws.get("is_home")):
        return error_command("workspace not found", tool_call_id)
    if not owner_only:
        from fastapi import HTTPException
        from src.dalton.settings import is_dalton_sharing_auth_enabled

        if is_dalton_sharing_auth_enabled():
            from src.dalton.sharing import require_workspace_access

            try:
                await require_workspace_access(
                    ws,
                    user_id,
                    channel="cos",
                    need=need,
                    workspace=ws,
                )
                return None
            except HTTPException as exc:
                if exc.status_code == 403 and need == "write":
                    try:
                        await require_workspace_access(
                            ws,
                            user_id,
                            channel="cos",
                            need="read",
                            workspace=ws,
                        )
                    except HTTPException:
                        return error_command("workspace not found", tool_call_id)
                    return error_command("该工作区对你只读", tool_call_id)
                return error_command("workspace not found", tool_call_id)
    # --- DALTON-HOOK(workspace-sharing-settings): end ---
    return error_command("workspace not found", tool_call_id)


async def verify_thread_owner(
    thread_id: str,
    user_id: str,
    tool_call_id: str,
    *,
    need: str = "read",
    owner_only: bool = False,
) -> Command | None:
    """Return error Command if user cannot access thread, else None."""
    from src.server.database.conversation.threads_read import (
        get_thread_auth_meta,
        get_thread_owner_id,
    )

    try:
        owner_id = await get_thread_owner_id(thread_id)
        if owner_id == user_id:
            # --- DALTON-HOOK(colleague-dispatch): begin ---
            # DALTON-HOOK(colleague-access): gate colleague workspace access on is_dalton_sharing_auth_enabled().
            from src.dalton.settings import is_dalton_sharing_auth_enabled

            if is_dalton_sharing_auth_enabled():
                from fastapi import HTTPException
                from src.dalton.sharing import require_workspace_access

                meta = await get_thread_auth_meta(thread_id)
                ws_owner_id = (meta or {}).get("workspace_owner_id")
                ws_id = (meta or {}).get("workspace_id")
                if ws_owner_id and str(ws_owner_id) != str(user_id):
                    if not ws_id:
                        return error_command(
                            "thread not found or not owned by user", tool_call_id
                        )
                    try:
                        await require_workspace_access(
                            str(ws_id),
                            user_id,
                            channel="cos",
                            need=need,
                        )
                    except HTTPException as exc:
                        if exc.status_code == 403 and need == "write":
                            try:
                                await require_workspace_access(
                                    str(ws_id),
                                    user_id,
                                    channel="cos",
                                    need="read",
                                )
                            except HTTPException:
                                return error_command(
                                    "thread not found or not owned by user",
                                    tool_call_id,
                                )
                            return error_command("该工作区对你只读", tool_call_id)
                        return error_command(
                            "thread not found or not owned by user", tool_call_id
                        )
            # --- DALTON-HOOK(colleague-dispatch): end ---
            return None
        # --- DALTON-HOOK(workspace-sharing-settings): begin ---
        # DALTON-HOOK(colleague-dispatch): pass workspace_owner_id and workspace_id for non-owner thread access checks.
        if not owner_only and owner_id is not None:
            from fastapi import HTTPException
            # DALTON-HOOK(colleague-access): gate colleague thread access on is_dalton_sharing_auth_enabled().
            from src.dalton.settings import is_dalton_sharing_auth_enabled

            if is_dalton_sharing_auth_enabled():
                from src.dalton.sharing import require_thread_access

                meta = await get_thread_auth_meta(thread_id)
                ws_owner_id = (meta or {}).get("workspace_owner_id")
                ws_id = (meta or {}).get("workspace_id")
                try:
                    await require_thread_access(
                        thread_id,
                        user_id,
                        channel="cos",
                        need=need,
                        owner_id=str(owner_id),
                        workspace_owner_id=str(ws_owner_id) if ws_owner_id else None,
                        workspace_id=str(ws_id) if ws_id else None,
                    )
                    return None
                except HTTPException as exc:
                    if exc.status_code == 403 and need == "write":
                        try:
                            await require_thread_access(
                                thread_id,
                                user_id,
                                channel="cos",
                                need="read",
                                owner_id=str(owner_id),
                                workspace_owner_id=str(ws_owner_id) if ws_owner_id else None,
                                workspace_id=str(ws_id) if ws_id else None,
                            )
                        except HTTPException:
                            return error_command(
                                "thread not found or not owned by user", tool_call_id
                            )
                        return error_command("该工作区对你只读", tool_call_id)
                    return error_command(
                        "thread not found or not owned by user", tool_call_id
                    )
        # --- DALTON-HOOK(workspace-sharing-settings): end ---
        return error_command(
            "thread not found or not owned by user", tool_call_id
        )
    except Exception as e:
        logger.error(f"Failed to verify thread ownership: {e}")
        return error_command("thread not found", tool_call_id)
    return None
