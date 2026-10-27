"""The Chief of Staff's tools: how Home's agent runs the user's workspaces.

Each workspace has its own agent, its analyst, and the Chief of Staff in Home
hands work to them rather than doing it in their folders. A hand-off always
names its workspace: a new one is created first, under its own approval or
one the user gave in advance, so the user agrees to the workspace before any
work is handed to it. It lists, creates and deletes workspaces but does not
stop them: Home runs on the computer a stop would take down. A delete is the
user's to confirm on its own card, never approved in advance. ``agent_output``
is how it reads a thread, so its ``manage_threads`` has no second way to.
"""

import logging
from typing import Annotated

from langchain.tools import InjectedState
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.types import Command

from ptc_agent.agent.limits import pending_read
from src.tools.secretary._commands import (
    InjectedToolCallId,
    error_command,
    success_command,
    verify_thread_owner,
)
from src.tools.secretary.approvals import preapproved
from src.tools.secretary.dispatch import dispatch
from src.tools.secretary.tools import (
    _workspaces_delete,
    threads_delete,
    threads_list,
    threads_stop,
    # DALTON-HOOK(workspace-sharing-settings): CoS file access on colleague workspaces
    workspace_file_read_cos,
    workspace_file_write_cos,
    workspace_files_list_cos,
    workspaces_create,
    workspaces_list,
)

logger = logging.getLogger(__name__)


def _timezone(configurable: dict) -> str:
    """The user's zone, which a run's label states its start time in."""
    return configurable.get("timezone") or "UTC"


@tool("manage_workspaces")
async def chief_of_staff_workspaces(
    action: str,
    config: RunnableConfig,
    state: Annotated[dict, InjectedState],
    name: str | None = None,
    description: str | None = None,
    workspace_id: str | None = None,
    # DALTON-HOOK(workspace-sharing-settings): optional path/content for CoS file read/write
    path: str | None = None,
    content: str | None = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """List the user's and colleagues' shared workspaces, create or delete the user's own workspace, or list/read/write files in a workspace.

    Use "list_files", "read_file", and "write_file" to inspect or update files in a colleague's
    shared workspace (which does not share this computer's filesystem); use "write_file" only
    when the workspace's access is "rw". You cannot delegate into a colleague's shared
    workspace — delegate_to_analyst refuses it; to act on that work, create a workspace of
    the user's own and hand off to its analyst.

    Args:
        action: "list", "create", "delete", "list_files", "read_file", or "write_file".
        name: Name for the new workspace; required for "create".
        description: What the new workspace is for; optional.
        workspace_id: Target workspace ID; required for "delete", "list_files", "read_file", and "write_file". Home cannot be deleted.
        path: Relative directory or file path INSIDE workspace_id's own folder (sources/x.md, not DASH/sources/x.md); optional prefix for "list_files", required for "read_file" and "write_file". Workspace folders sit beside Home (../<folder>), never inside it — a path starting with a workspace folder name is refused, because it would silently land in the wrong workspace.
        content: Full UTF-8 text to write; required for "write_file" (requires "rw" access).

    Returns:
        Workspace rows (with colleague owner and access level) for "list"; for "create" the new id with `folder_path` and `folder_ready` (before copying or cleaning into a new workspace, confirm `folder_path` exists and is non-empty — until `folder_ready` is true the folder is not on the machine yet); file paths/content for file actions.
        (DALTON-HOOK(workspace-folder-ready): the create result's folder fields.)
    """
    user_id = config.get("configurable", {}).get("user_id")
    if not user_id:
        return error_command("user_id not found in config", tool_call_id)
    if action == "list":
        return await workspaces_list(user_id, tool_call_id)
    if action == "create":
        return await workspaces_create(
            user_id, name, description, tool_call_id,
            preapproved=preapproved(state, tool_call_id),
        )
    if action == "delete":
        return await _workspaces_delete(user_id, workspace_id, tool_call_id)
    # --- DALTON-HOOK(workspace-sharing-settings): begin ---
    if action == "list_files":
        return await workspace_files_list_cos(
            user_id, workspace_id, path, tool_call_id
        )
    if action == "read_file":
        return await workspace_file_read_cos(
            user_id, workspace_id, path, tool_call_id
        )
    if action == "write_file":
        return await workspace_file_write_cos(
            user_id, workspace_id, path, content, tool_call_id
        )
    # --- DALTON-HOOK(workspace-sharing-settings): end ---
    return error_command(
        f"Unknown action: {action}. Use list, create, delete, list_files, read_file, or write_file.",
        tool_call_id,
    )


@tool("delegate_to_analyst")
async def delegate_to_analyst(
    question: str,
    config: RunnableConfig,
    state: Annotated[dict, InjectedState],
    workspace_id: str | None = None,
    thread_id: str | None = None,
    report_back: bool = True,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """Hand work to a workspace's analyst, who does it there in the background unless the user declines.

    Needs workspace_id or thread_id: a workspace that does not exist yet is
    created with manage_workspaces first.

    Args:
        question: The task, written for the analyst, who does not see this conversation.
        workspace_id: The workspace whose analyst starts a new thread. Ignored with thread_id.
        thread_id: An analyst's thread to continue. While it is mid-turn, the analyst takes the task in before its next step, or as its next turn if that one ends first.
        report_back: Leave True to be told when the analyst finishes, is stopped or fails.
            False only when the user asks for no report, or will follow the work in that
            workspace themselves.

    Returns:
        The analyst's thread, still running; its report_back field says whether a report-back is coming.
    """
    if not workspace_id and not thread_id:
        return error_command(
            "No workspace named. Pass workspace_id, or thread_id to continue "
            "a thread. For work no workspace covers, create one with "
            'manage_workspaces(action="create") first, then hand off to it.',
            tool_call_id,
        )
    return await dispatch(
        question, config, workspace_id, thread_id, report_back, tool_call_id,
        preapproved=preapproved(state, tool_call_id),
    )


@tool("agent_output")
async def agent_output(
    thread_id: str,
    config: RunnableConfig,
    turns: int = 1,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """Read what a thread's agent wrote, in any of the user's workspaces, while it runs or after.

    Args:
        thread_id: The thread to read.
        turns: How many of the most-recent turns to return. Default 1 (only the
            latest turn's output). Pass a larger N for the last N turns, or 0
            for recent history (up to the 50 most recent turns); multiple turns
            are separated by '---'. A turn still streaming returns only that
            live turn.

    Returns:
        The agent's reply text, not its tool calls, each run under a line with its start time, how it ended and whether it was a report-back, and whether the thread is still running.
    """
    configurable = config.get("configurable", {})
    user_id = configurable.get("user_id")
    if not user_id:
        return error_command("user_id not found in config", tool_call_id)

    if err := await verify_thread_owner(thread_id, user_id, tool_call_id):
        return err

    from src.tools.secretary.utils import extract_text_from_thread

    try:
        result = await extract_text_from_thread(
            thread_id, turns, timezone=_timezone(configurable)
        )
    except Exception as e:
        logger.error(f"Failed to extract text from thread {thread_id}: {e}")
        return error_command("failed to retrieve thread output", tool_call_id)

    # A read of a thread mid-run is marked for the loop guard, which cannot
    # tell a poll from the text: a live run's text grows between reads.
    running = result.get("status") == "running"
    return success_command(result, tool_call_id, pending_read() if running else None)


@tool("manage_threads")
async def chief_of_staff_threads(
    action: str,
    config: RunnableConfig,
    workspace_id: str | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """List the user's threads, stop the turn one is running, or delete one once the user confirms.

    Args:
        action: "list", "stop" or "delete". "stop" ends the turn within a few seconds;
            the thread and what it produced so far stay.
        workspace_id: For "list", only this workspace's threads; all of them without it.
        thread_id: The thread to stop or delete; required for both.
        run_id: For "stop", the run_id from the hand-off's delegate_to_analyst result, so
            the stop never ends a turn started there since; omit it to stop whatever the
            thread is running.
    """
    configurable = config.get("configurable", {})
    user_id = configurable.get("user_id")
    if not user_id:
        return error_command("user_id not found in config", tool_call_id)
    if action == "list":
        return await threads_list(
            user_id, workspace_id, tool_call_id, configurable.get("thread_id")
        )
    if action == "stop":
        return await threads_stop(
            user_id,
            thread_id,
            configurable.get("thread_id"),
            tool_call_id,
            run_id=run_id,
        )
    if action == "delete":
        return await threads_delete(user_id, thread_id, tool_call_id)
    return error_command(
        f"Unknown action: {action}. Use list, stop or delete.", tool_call_id
    )


CHIEF_OF_STAFF_TOOLS = [
    chief_of_staff_workspaces,
    delegate_to_analyst,
    agent_output,
    chief_of_staff_threads,
]
