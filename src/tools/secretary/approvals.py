"""Hand-offs and new workspaces the user approved in advance.

The setting lives in ``other_preference``, which the agent cannot write. It is
read when the model proposes the call and recorded against that call in the
thread state, never when the tool runs: a tool that paused re-runs from the top
on resume, so a setting changed while its card was pending would otherwise
skip an answer the user already gave, and a hand-off they declined would run.
"""

import logging
import re
from typing import Annotated, Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.messages import AIMessage
from typing_extensions import NotRequired

logger = logging.getLogger(__name__)

AUTO_APPROVE_HANDOFFS = "auto_approve_handoffs"
AUTO_APPROVE_WORKSPACE_CREATION = "auto_approve_workspace_creation"
AUTO_APPROVE_KEYS = (AUTO_APPROVE_HANDOFFS, AUTO_APPROVE_WORKSPACE_CREATION)

# DALTON-HOOK(workspace-duplicate-guard): a company workspace is named for its
# ticker, so two spellings of one listing are one workspace. The shape gate
# keeps the symbol spelling out of ordinary names: the fold inside
# ``display_spelling`` reads ``计划①`` and ``计划1`` as one string, which for a
# name that is not a ticker equates two names nobody equated.
_TICKER_SHAPED = re.compile(r"^[A-Z0-9^=\-&]{1,12}(?:\.[A-Z]{1,5})?$")


def is_duplicate_name(new: str, existing: str) -> bool:
    """Whether ``new`` names the same workspace as ``existing``.

    The same name ignoring case and outer whitespace; for names that read as
    tickers, additionally the same listing after the symbol spelling is
    normalized (``700.HK`` and ``0700.HK``, ``600519.SS`` and ``600519.SH``).
    """
    if new.strip().casefold() == existing.strip().casefold():
        return True
    if not (
        _TICKER_SHAPED.match(new.strip().upper())
        and _TICKER_SHAPED.match(existing.strip().upper())
    ):
        return False
    try:
        from src.server.models.user import normalize_symbol

        return normalize_symbol(new) == normalize_symbol(existing)
    except Exception:
        # A name that cannot be read as a symbol is only the plain rule.
        return False


async def duplicate_workspace(user_id: str, name: str) -> tuple[dict | None, bool]:
    """The user's live workspace ``name`` would duplicate, and whether the read
    behind that answer ran.

    A read that fails reports ``(None, False)`` so the caller asks rather than
    trusts "no duplicate" — the same fail-closed side as a setting that cannot
    be read. The rows carry the live-workspace fence the name-key lookups use.
    """
    from src.server.database.workspace import get_live_workspace_names

    try:
        holders = await get_live_workspace_names(user_id)
    except Exception:
        logger.error(
            f"Could not read workspaces for the duplicate check of {name!r}; asking",
            exc_info=True,
        )
        return None, False
    for holder in holders:
        if is_duplicate_name(name, holder.get("name") or ""):
            return holder, True
    return None, True


async def _standing_create_withheld(user_id: str, call: dict[str, Any]) -> bool:
    """Whether a workspace create approved in advance must still ask.

    True when it would duplicate a live workspace, or when the read that would
    tell cannot run. Decided here, at proposal time, for the same reason the
    setting is: a tool that paused re-runs from the top on resume, so a
    duplicate deleted while its card was pending must not hand the create back
    to the standing approval the user's answer already replaced.
    """
    args = call.get("args")
    name = args.get("name") if isinstance(args, dict) else None
    if not isinstance(name, str) or not name.strip():
        # The tool refuses a create without a name before any card is drawn.
        return False
    holder, confirmed = await duplicate_workspace(user_id, name)
    return holder is not None or not confirmed


def _settings_for(call: dict[str, Any]) -> str | None:
    """The setting that, granted, lets ``call`` run without its card."""
    args = call.get("args")
    if not isinstance(args, dict):
        args = {}
    name = call.get("name")
    if name == "delegate_to_analyst":
        return AUTO_APPROVE_HANDOFFS
    if name == "manage_workspaces" and args.get("action") == "create":
        return AUTO_APPROVE_WORKSPACE_CREATION
    return None


async def _granted(user_id: str) -> set[str]:
    from src.server.database.user import get_user_preferences

    try:
        prefs = await get_user_preferences(user_id)
    except Exception:
        # Asking is the default, so a setting that cannot be read shows the
        # card instead of failing the turn.
        logger.warning(
            f"Could not read approval settings for user {user_id}; asking",
            exc_info=True,
        )
        return set()
    other = (prefs or {}).get("other_preference") or {}
    return {key for key in AUTO_APPROVE_KEYS if other.get(key) is True}


class StandingApprovalState(AgentState):
    # Every model call proposing an approvable call rewrites this list, and
    # only those tools read it, so an id left from an earlier model call can
    # only meet a tool that never looks.
    preapproved_calls: NotRequired[Annotated[list[str], PrivateStateAttr]]


class StandingApprovalMiddleware(AgentMiddleware):
    """Records which of a model call's tool calls need no approval card."""

    state_schema = StandingApprovalState

    def __init__(self, user_id: str | None) -> None:
        super().__init__()
        self._user_id = user_id

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or not self._user_id:
            return None
        wanted = {
            call["id"]: needed
            for call in last.tool_calls
            if (needed := _settings_for(call))
        }
        if not wanted:
            return None
        granted = await _granted(self._user_id)
        by_id = {call["id"]: call for call in last.tool_calls}
        preapproved_calls = []
        for call_id, needed in wanted.items():
            if needed not in granted:
                continue
            # DALTON-HOOK(workspace-duplicate-guard): a duplicate, or a check
            # that could not run, withholds the standing approval here.
            if needed is AUTO_APPROVE_WORKSPACE_CREATION and await _standing_create_withheld(
                self._user_id, by_id[call_id]
            ):
                continue
            preapproved_calls.append(call_id)
        return {"preapproved_calls": preapproved_calls}


def preapproved(state: dict[str, Any], tool_call_id: str) -> bool:
    """Whether this call was approved in advance when the model proposed it."""
    return tool_call_id in (state.get("preapproved_calls") or ())
