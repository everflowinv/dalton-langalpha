"""The Chief of Staff's tools: how Home's agent runs the user's workspaces.

# DALTON-HOOK(computer-cli-tokens-off): retain server-side workspace/thread tools.
Each workspace has its own agent, its analyst, and the Chief of Staff in Home
hands work to them rather than doing it in their folders. A hand-off always
names its workspace: a new one is created first with ``manage_workspaces``,
under its own approval or one the user gave in advance, so the user agrees to
the workspace before any work is handed to it. ``agent_output`` is how it
reads a thread, so ``manage_threads`` has no second way to.
"""

import logging
from typing import Annotated

from langchain.tools import InjectedState
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.types import Command

# DALTON-HOOK(computer-cli-tokens-off): pending_read is used by agent_output below.
from ptc_agent.agent.limits import pending_read
from src.llms.reasoning import DelegatedEffort
from src.tools.secretary._commands import (
    HANDOFF_TOOL_NAME,
    InjectedToolCallId,
    error_command,
    success_command,
    verify_thread_owner,
)
# DALTON-HOOK(cos-settings-tools): import approval_snapshot for frozen create snapshots
from src.tools.secretary.approvals import approval_snapshot, preapproved
from src.tools.secretary.dispatch import dispatch
# DALTON-HOOK(computer-cli-tokens-off): retain server-side CoS tools when la CLI is off.
# DALTON-HOOK(cos-settings-tools): wire update action into manage_workspaces.
from src.tools.secretary.tools import (
    _workspaces_delete,
    threads_delete,
    threads_list,
    threads_stop,
    # DALTON-HOOK(workspace-sharing-settings): CoS file access on colleague workspaces
    workspace_file_copy_cos,
    workspace_file_read_cos,
    workspace_file_write_cos,
    workspace_files_list_cos,
    workspaces_create,
    workspaces_list,
    workspaces_update,
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
    # DALTON-HOOK(cos-settings-tools): optional target computer name/id for "create"
    computer: str | None = None,
    # DALTON-HOOK(workspace-sharing-settings): optional path/content for CoS file read/write
    path: str | None = None,
    content: str | None = None,
    dest_path: str | None = None,
    dest_workspace_id: str | None = None,
    # DALTON-HOOK(colleague-access): copy_file docstring below covers strict UTF-8 text files up to 5 MB alongside binary files.
    # DALTON-HOOK(workspace-dedup): override duplicate company workspace check only after explicit user confirmation
    allow_duplicate_company: bool = False,
    # DALTON-HOOK(workspace-dedup): optional coverage unit auto-registration fields for "create"
    coverage_kind: str | None = None,
    ticker: str | None = None,
    display_name: str | None = None,
    skip_coverage_suggestion: bool = False,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """List the user's and colleagues' shared workspaces, create/update/delete a workspace, or list/read/copy/write files in a workspace.

    For colleague-shared workspaces (separate filesystem): use "list_files"/"read_file" (text only),
    "copy_file" to copy binaries (Excel, PDF, images) into Home or another own workspace before
    sending or linking via folder_path, and "write_file" only when access is "rw".
    Limits: "copy_file" allows Office (.xlsx, .docx, .pptx, .xls, .doc; legacy .ppt is refused —
    ask the owner to save as .pptx), PDF (.pdf), and images (.png, .jpg, .jpeg, .gif, .webp, .bmp,
    .tiff) up to 50 MB, plus strict UTF-8 text (.md, .markdown, .txt, .csv, .tsv, .json, .jsonl,
    .yaml, .yml, .py, .sql, .html, .xml, .log) up to 5 MB; all other types (archives,
    pickle/database, audio/video) are refused.
    Dispatching into a colleague-shared workspace via delegate_to_analyst requires colleague
    dispatch enabled and "rw" access (follow current access returned by this tool); otherwise
    create the user's own workspace first.

    Args:
        action: "list", "create", "update", "delete", "list_files", "read_file", "copy_file", or "write_file". Use "delete" only on explicit user request after verifying the name and waiting for confirmation.
        name: Workspace name; required for "create", optional for "update" (omit to keep current; "update" needs name or description). Renaming also changes its folder name on its computer; Home cannot be renamed.
        description: Optional description (up to 1,000 chars) for "create"/"update" (omit on "update" to keep current).
        workspace_id: Target workspace ID; required for "update", "delete", and file actions. Home cannot be deleted.
        computer: Optional target computer for "create": display name from `<activity>` or `manage_workspaces(action="list")`, full UUID, or unique 8+ char prefix. Omit for primary computer (or first-time cloud primary setup when supported). Own machines always show a confirmation card.
        path: Relative path inside workspace_id's own folder (e.g. sources/x.md, never prefixed with a workspace folder name); optional prefix for "list_files", required for "read_file", "copy_file", and "write_file".
        content: Full UTF-8 text for "write_file" (requires "rw" access).
        dest_path: Destination path for "copy_file", relative to destination folder (default shared/<filename>).
        dest_workspace_id: Optional destination workspace for "copy_file" (default Home); must be one of the user's own workspaces.
        allow_duplicate_company: For "create" only: set True only after explicit user confirmation following a `company_workspace_exists` refusal (default False).
        coverage_kind: Optional for "create": "company", "sector", or "none" (default). When "company"/"sector", registers and binds a team coverage unit (internal/admin only).
        ticker: Optional stock ticker for "create" with `coverage_kind="company"` (e.g. "NVDA", "0700.HK"; omit for unlisted companies or sectors).
        display_name: Optional company/sector display name for "create" when `coverage_kind` is "company"/"sector" (defaults to `name`).
        skip_coverage_suggestion: For "create" only: set True when retrying after `coverage_suggestion` and the user explicitly declines registering a coverage unit (default False).

    Returns:
        Workspace rows (id/folder/computer/access) for "list"; new id with `folder_path`/`folder_ready` (verify `folder_path` exists and `folder_ready` is true before copying into it) and `coverage_unit` for "create"; updated metadata for "update"; or file paths/content/size for file actions.
    """
    # DALTON-HOOK(workspace-folder-ready): the create result's folder fields are documented above.
    # DALTON-HOOK(cos-settings-tools): support manage_workspaces(action="update").
    # DALTON-HOOK(colleague-access): copy_file docstring above covers UTF-8 text files up to 5 MB alongside binary files.
    user_id = config.get("configurable", {}).get("user_id")
    if not user_id:
        return error_command("user_id not found in config", tool_call_id)
    if action == "list":
        return await workspaces_list(user_id, tool_call_id)
    if action == "create":
        return await workspaces_create(
            user_id, name, description, tool_call_id,
            preapproved=preapproved(state, tool_call_id),
            # DALTON-HOOK(cos-settings-tools): pass target computer and frozen confirmation snapshot
            computer=computer,
            snapshot=approval_snapshot(state, tool_call_id),
            # DALTON-HOOK(workspace-dedup): pass explicit duplicate override and coverage registration fields
            allow_duplicate_company=allow_duplicate_company,
            coverage_kind=coverage_kind,
            ticker=ticker,
            display_name=display_name,
            skip_coverage_suggestion=skip_coverage_suggestion,
        )
    # --- DALTON-HOOK(cos-settings-tools): begin ---
    if action == "update":
        return await workspaces_update(
            user_id, workspace_id, name, description, tool_call_id
        )
    # --- DALTON-HOOK(cos-settings-tools): end ---
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
    if action == "copy_file":
        return await workspace_file_copy_cos(
            user_id, workspace_id, path, dest_path, tool_call_id,
            dest_workspace_id=dest_workspace_id,
        )
    if action == "write_file":
        return await workspace_file_write_cos(
            user_id, workspace_id, path, content, tool_call_id
        )
    # --- DALTON-HOOK(workspace-sharing-settings): end ---
    # DALTON-HOOK(cos-settings-tools): list update in unknown-action error
    return error_command(
        f"Unknown action: {action}. Use list, create, update, delete, list_files, "
        "read_file, copy_file, or write_file.",
        tool_call_id,
    )


@tool(HANDOFF_TOOL_NAME)
async def delegate_to_analyst(
    question: str,
    config: RunnableConfig,
    state: Annotated[dict, InjectedState],
    workspace_id: str | None = None,
    thread_id: str | None = None,
    report_back: bool = True,
    model: str | None = None,
    effort: DelegatedEffort | None = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """Hand work to a workspace's analyst, who does it there in the background unless the user declines.

    Needs workspace_id or thread_id: a workspace that does not exist yet is
    created with manage_workspaces(action="create") first.

    Args:
        question: The task, written for the analyst, who does not see this conversation.
        workspace_id: The workspace whose analyst starts a new thread. Ignored with thread_id.
        thread_id: An analyst's thread to continue. While it is mid-turn, the analyst takes the task in before its next step, or as its next turn if that one ends first.
        report_back: Leave True to be told when the analyst finishes, is stopped or fails.
            False only when the user asks for no report, or will follow the work in that
            workspace themselves.
        model: Optional model key to run the analyst on. Pass one only when the user asks
            for a model or a skill's procedure names one; it stays the thread's model for
            later turns. Omitted, the turn runs the thread's own model, or the user's
            default on a new thread. A thread mid-turn takes it only into a turn already
            running that model, and otherwise refuses it until that turn ends.
        effort: How much work the analyst puts into this turn, read as Task's effort
            is. Omitted, the thread's own effort, else the user's setting. A level the
            model lacks runs at the next one below, and the result says which. For this
            turn only; a thread mid-turn refuses it until that turn ends.

    Returns:
        The analyst's thread, still running; its report_back field says whether a report-back is coming.
    """
    # DALTON-HOOK(computer-cli-tokens-off): direct user-facing error to manage_workspaces when la CLI is off.
    if not workspace_id and not thread_id:
        return error_command(
            "No workspace named. Pass workspace_id, or thread_id to continue "
            "a thread. For work no workspace covers, create one with "
            'manage_workspaces(action="create") first, then hand off to it.',
            tool_call_id,
        )
    return await dispatch(
        question, config, workspace_id, thread_id, report_back, tool_call_id,
        preapproved=preapproved(state, tool_call_id), model=model, effort=effort,
    )


# DALTON-HOOK(computer-cli-tokens-off): retain agent_output and manage_threads alongside delegate_to_analyst.
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

    from src.server.services.thread_output import extract_text_from_thread

    try:
        output = await extract_text_from_thread(
            thread_id, turns, timezone=_timezone(configurable)
        )
    except Exception as e:
        logger.error(f"Failed to extract text from thread {thread_id}: {e}")
        return error_command("failed to retrieve thread output", tool_call_id)

    # Upstream returns a ThreadOutput (None once the thread is deleted mid-read);
    # the tool keeps its dict result: text, status, thread_id, workspace_id.
    if output is None:
        result = {"text": "", "status": "not_found", "thread_id": thread_id, "workspace_id": ""}
    else:
        result = {"thread_id": thread_id, **output._asdict()}
        result["workspace_id"] = result["workspace_id"] or ""

    # A read of a thread mid-run is marked for the loop guard, which cannot
    # tell a poll from the text: a live run's text grows between reads.
    running = result["status"] == "running"
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
