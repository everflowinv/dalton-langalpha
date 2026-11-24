"""A thread's agent, resolved and built as its turns resolve and build it.

A model call made between turns (manual /compact, the hand-off at a model
switch) is checked by the provider against the turn's own request: the
prompt, the tools and the thinking it was sent with, which its cache and the
thinking in the kept turns are verified against. So a turn and a call between
turns read those inputs here, and an input added for one reaches the other.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from src.server.services.shared_workspaces.turn import TurnIdentity

if TYPE_CHECKING:
    from ptc_agent.agent.claude_code.launcher import Launcher
    from ptc_agent.agent.middleware.member_context import MemberContext
    from ptc_agent.agent.roles import AgentRole
    from ptc_agent.config.agent import Admitted, AgentConfig
    from ptc_agent.core.claude_code_clis import CliQueue
    from ptc_agent.core.project_context import ProjectContext
    from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
    from src.server.services.computer_manager._types import WorkspaceToolView
    from src.server.services.egress.direct_tools import DirectMCPBinding
    from src.server.services.livefs.history_files import ThreadHistory
    from src.server.services.turn_runtime import TurnRoute


# DALTON-HOOK(automation-effort): sentinel distinguishing omitted vs explicit reasoning_effort_override
_UNSET_EFFORT: Any = object()


def thread_resolver(
    user_id: str,
    thread_id: str,
    route: TurnRoute,
    *,
    identity: TurnIdentity,
    thread: Mapping[str, Any] | None,
    is_byok: bool,
    reasoning_effort: str | None = None,
    fast_mode: bool | None = None,
    enabled_subagents: list[str] | None = None,
    ignore_custom_models: bool = False,  # DALTON-HOOK(cos-settings-tools)
    run_metadata: Mapping[str, Any] | None = None,  # DALTON-HOOK(cos-settings-tools)
) -> Callable[..., Awaitable[AgentConfig]]:
    """Maps a model name (None for the account default) to the config a turn
    on the thread runs it under, in the route's workspace and role.

    A turn naming no effort or fast mode runs the thread's own (``thread``,
    its row), picked in its composer, and with none of those the user's
    Settings. A client sends what its composer shows, so the row decides only
    for a turn no composer started: a report-back, a resume after a reload, a
    call between turns.
    """
    from src.server.app import setup
    from src.server.services.llm.config import resolve_llm_config

    row = thread or {}
    effort = reasoning_effort or row.get("reasoning_effort")
    fast = fast_mode if fast_mode is not None else row.get("fast_mode")

    async def resolve(
        model: str | None,
        *,
        reasoning_effort_override: Any = _UNSET_EFFORT,  # DALTON-HOOK(automation-effort)
        ignore_custom_models_override: bool | None = None,  # DALTON-HOOK(cos-settings-tools)
    ) -> AgentConfig:
        effective_effort = (
            effort
            if reasoning_effort_override is _UNSET_EFFORT
            else reasoning_effort_override
        )
        # DALTON-HOOK(cos-settings-tools): resolve against this run's metadata marker when provided
        base_ignore_custom = bool(
            ignore_custom_models
            or (run_metadata is not None and run_metadata.get("llm_model_system_only"))
        )
        effective_ignore_custom = (
            base_ignore_custom
            if ignore_custom_models_override is None
            else bool(ignore_custom_models_override)
        )
        return await resolve_llm_config(
            setup.agent_config,
            user_id,
            model,
            is_byok,
            reasoning_effort=effective_effort,
            fast_mode=fast,
            thread_id=thread_id,
            enabled_subagents=enabled_subagents,
            workspace_id=route.workspace_id,
            identity=identity,
            role=route.role,
            # DALTON-HOOK(cos-settings-tools): only forward ignore_custom_models when True so normal calls stay identical to upstream
            **({"ignore_custom_models": True} if effective_ignore_custom else {}),
        )

    return resolve


async def thread_route(thread_id: str, user_id: str) -> tuple[dict[str, Any], TurnRoute]:
    """The thread's row and where its next turn runs, decided as a send on
    the thread decides it."""
    from src.server.database.conversation import get_thread_auth_meta
    from src.server.database.workspace import get_workspace
    from src.server.services.turn_runtime import turn_route

    row = await get_thread_auth_meta(thread_id)
    if not row or not row.get("workspace_id"):
        raise HTTPException(status_code=404, detail=f"Thread not found: {thread_id}")
    workspace = await get_workspace(str(row["workspace_id"]))
    return row, turn_route(user_id, workspace)


@dataclass(frozen=True)
class AgentInputs:
    """What the agent is built from that its prompt and tools depend on, read
    as a turn reads it. A run adds what only it has (its saver, its guard,
    its turn row, the order ledger) beside ``build_kwargs``."""

    #: The sender, and the workspace's owner: whose connectors are bound,
    #: whose vault is redacted, and whose memory and files the agent mounts.
    identity: TurnIdentity
    thread_id: str
    workspace_id: str
    role: AgentRole
    #: The workspace folder the turn runs in; the run binds it too.
    project: ProjectContext | None
    #: The project's frozen registry and summary: the session's own fields
    #: belong to whichever sibling on the machine resolved last.
    tool_view: WorkspaceToolView
    #: The turn's profile (``TurnIdentity.profile``), naming the trading
    #: rule the tools bring.
    user_profile: dict[str, Any] | None
    #: The directly bound tools, unopened: a run opens them (``drive``).
    direct_mcp: DirectMCPBinding
    #: The computer the session runs on, where a Claude Code turn's CLI may.
    computer_id: str | None
    # DALTON-HOOK(colleague-dispatch): workspace owner identity (B) distinct from turn caller (A)
    workspace_owner_id: str | None = None
    #: The thread's own transcript, for the file tools of a turn in a thread
    #: its sender keeps private in a shared workspace (``ThreadHistory``).
    thread_history: ThreadHistory | None = None
    #: In a shared workspace, the member whose own context the turn carries
    #: (``shared_workspaces_member_context``); None for a call between
    #: turns and for every other turn.
    member_context: MemberContext | None = None

    @classmethod
    async def read(
        cls,
        session: Any,
        *,
        identity: TurnIdentity,
        workspace_id: str,
        thread_id: str,
        role: AgentRole,
        user_profile: dict[str, Any] | None,
        workspace_owner_id: str | None = None,
        acting_for: str | None = None,
        member_context: MemberContext | None = None,
    ) -> AgentInputs:
        """``workspace_id`` is the one whose folder the turn runs in;
        ``acting_for`` the thread's, when that is another (Home's turn in its
        stand-in)."""
        from dataclasses import replace

        from src.server.services.egress.direct_tools import direct_tools_for_turn
        from src.server.services.livefs.history_files import private_thread_history
        from src.server.services.trading_rule import with_trading_rule
        from src.server.services.workspace_layout import resolve_project_context
        from src.server.services.workspace_manager import WorkspaceManager

        # DALTON-HOOK(colleague-dispatch): resolve workspace owner B and pass dual identity to direct MCP
        eff_owner_id = (
            identity.owner if identity.shared else (workspace_owner_id or identity.owner)
        )
        tool_view = WorkspaceManager.get_instance().tool_view(session, workspace_id)
        direct_kwargs: dict[str, Any] = {}
        if not identity.shared and identity.sender and eff_owner_id != identity.sender:
            direct_kwargs["owner_user_id"] = eff_owner_id
            direct_kwargs["caller_user_id"] = identity.sender
        direct_mcp = await direct_tools_for_turn(
            session,
            user_id=eff_owner_id,
            workspace_id=workspace_id,
            view=tool_view,
            **direct_kwargs,
        )
        project = await resolve_project_context(session, workspace_id)
        if project is not None and acting_for and acting_for != workspace_id:
            project = replace(project, acting_for=acting_for)
        return cls(
            identity=identity,
            # DALTON-HOOK(colleague-dispatch): store workspace owner identity
            workspace_owner_id=eff_owner_id,
            thread_id=thread_id,
            workspace_id=workspace_id,
            role=role,
            project=project,
            tool_view=tool_view,
            # Off the resolve, not the bind, so it moves only when a setting does.
            user_profile=with_trading_rule(user_profile, tool_view.trading_rule),
            # DALTON-HOOK(colleague-dispatch): bind direct_mcp resolved above
            # The connectors are the workspace owner's, as on the computer.
            direct_mcp=direct_mcp,
            computer_id=session.computer_id,
            thread_history=(
                await private_thread_history(
                    workspace_id,
                    project.dir_name if project is not None else None,
                    thread_id,
                    identity.sender,
                )
                if identity.shared and thread_id
                else None
            ),
            member_context=member_context,
        )

    def build_kwargs(self) -> dict[str, Any]:
        """The arguments ``build_ptc_graph_with_session`` takes from these."""
        from src.server.app import setup
        from src.server.services.preview_links import cache_tool_preview

        # DALTON-HOOK(colleague-dispatch): forward workspace_owner_id when distinct from caller
        kwargs: dict[str, Any] = {
            "user_id": self.identity.sender,
            "owner_id": self.identity.owner,
            "own_data": not self.identity.shared,
            "user_profile": self.user_profile,
            "thread_id": self.thread_id,
            "store": setup.store,
            "on_signed_url": partial(cache_tool_preview, workspace_id=self.workspace_id),
            "direct_mcp": self.direct_mcp,
            "project": self.project,
            "tool_view": self.tool_view,
            "role": self.role,
            "claude_code_launcher": self.claude_code_launcher,
            "thread_history": self.thread_history,
            "member_context": self.member_context,
        }
        # DALTON-HOOK(colleague-dispatch): pass workspace_owner_id to graph build
        if (
            self.workspace_owner_id
            and not self.identity.shared
            and self.workspace_owner_id != self.identity.sender
        ):
            kwargs["workspace_owner_id"] = self.workspace_owner_id
        return kwargs

    def claude_code_launcher(
        self, sandbox: PTCSandbox, admitted: Admitted, queue: CliQueue | None = None, *,
        slot_wait_s: float | None = None,
    ) -> Launcher:
        """Where a Claude Code CLI runs, a turn's or a subagent's, and the token
        it signs in with, by what admission let it run on (``admitted``).
        ``slot_wait_s`` is how long a CLI on an own computer waits for a slot
        there, a subagent's shorter than a turn's, and ``queue`` is told while
        it waits. Imported here, never at the top: the SDK loads only for such a CLI."""
        from src.server.services.claude_code.launchers import turn_launcher

        return turn_launcher(
            sandbox, computer_id=self.computer_id, workspace_id=self.workspace_id, user_id=self.identity.sender,
            admitted=admitted, slot_wait_s=slot_wait_s, queue=queue,
        )
