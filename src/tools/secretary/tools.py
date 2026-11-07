"""The workspace and thread actions behind the Chief of Staff's tools.

The tools themselves are in ``chief_of_staff``; a hand-off runs through
``dispatch``.
"""

import asyncio
import io
import json
import logging
import os
import re
import sys
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from ptc_agent.agent.limits import pending_read
from src.server.contracts.status import is_live
# DALTON-HOOK(workspace-duplicate-guard): the duplicate read behind the create card
from src.tools.secretary.approvals import duplicate_workspace
from src.tools.secretary._commands import (
    decline_command,
    decline_routing_command,
    error_command,
    hitl_confirm,
    success_command,
    verify_thread_owner,
    verify_workspace_owner,
)
from src.utils.nested import without_keys

logger = logging.getLogger(__name__)

# The tools take the user from the run config, so the owner's user_id in a
# listed row is nothing the model needs or passes back. The row's own ids, which
# it does pass back, stay.
_OWNER_ID = frozenset({"user_id"})

_STOPPING = (
    "Stopping; the turn ends within a few seconds. If it was a hand-off with "
    "a report-back, that report will say how far it got: tell the user it is "
    "stopping and do not read the thread now."
)

# A workspace as a listing shows it: what a hand-off is routed by, and the
# status a stop or a "what is running" answer reads. A listing comes before
# every hand-off, so the row's settings, artifacts and machine bindings stay out.
_LISTED_WORKSPACE_FIELDS = (
    "workspace_id",
    "name",
    "description",
    "dir_name",
    "status",
    "created_at",
    "last_activity_at",
)


async def workspaces_list(user_id: str, tool_call_id: str) -> Command:
    """List workspaces for the user.

    A row names its folder, which sits at the root of its computer, and its
    computer, with whether that is the user's own, only when the user has
    more than one; on another computer than Home's, the folder's whole path
    too, since calls there take that machine's own paths.
    """
    try:
        from src.server.database.workspace import get_workspaces_for_user
        from src.tools.secretary.utils import read_computer_placement

        # Every workspace, since one left off the list is one the agent tells
        # the user does not exist. Pinned, then most recently used: the one a
        # hand-off is for is far likelier to be recent than early in the
        # user's custom order.
        (workspaces, _), placement = await asyncio.gather(
            get_workspaces_for_user(user_id=user_id, limit=None, sort_by="activity"),
            read_computer_placement(user_id),
        )
        rows = []
        for ws in workspaces:
            row = {field: ws.get(field) for field in _LISTED_WORKSPACE_FIELDS}
            if path := placement.path(ws):
                row["path"] = path
            if placement.computers:
                row["computer"] = placement.computer(ws)
            if machine := placement.machine(ws):
                row["machine"] = machine
            rows.append(row)
        payload: dict[str, object] = {"success": True, "workspaces": rows}
        # --- DALTON-HOOK(workspace-sharing-settings): begin ---
        # DALTON-HOOK(colleague-dispatch): adjust colleague_workspaces_note when colleague_dispatch is enabled.
        from src.dalton.settings import (
            is_colleague_dispatch_enabled,
            is_dalton_supabase_auth_enabled,
        )

        if is_dalton_supabase_auth_enabled():
            from src.dalton.sharing import list_colleague_cos_workspaces

            colleague_list = await list_colleague_cos_workspaces(user_id)
            payload["colleague_workspaces"] = colleague_list
            if colleague_list:
                if is_colleague_dispatch_enabled():
                    payload["colleague_workspaces_note"] = (
                        "同事共享的工作区：access 为 rw 时可用 list_files / read_file / "
                        "write_file，也可用 delegate_to_analyst 向该工作区的分析师派发任务；"
                        "access 为 ro（read_only=true）时只能用 list_files / read_file 读取，"
                        "不能向只读工作区派发任务。read_file 只读文本；要取工作区里的二进制"
                        "文件（Excel、PDF 等），用 copy_file 拷到 Home 后再发送。"
                    )
                else:
                    # Said here, where the model reads the list it is about to
                    # route by: promising a dispatch into one of these was the
                    # incident, and dispatch refuses them.
                    payload["colleague_workspaces_note"] = (
                        "同事共享的工作区当前只能读取（list_files / read_file；rw 时可 "
                        "write_file 写文件；二进制文件用 copy_file 拷到 Home），"
                        "delegate_to_analyst 不能向同事的工作区派发"
                        "任务（会被拒绝）。要在同事工作区里做的事：用 manage_workspaces"
                        '(action="create") 在用户自己名下新建工作区，再把任务派发给它的'
                        "分析师。"
                    )
        # --- DALTON-HOOK(workspace-sharing-settings): end ---
        content = json.dumps(payload, default=str)
    except Exception as e:
        logger.error(f"Failed to list workspaces: {e}")
        content = json.dumps({"success": False, "error": "failed to list workspaces"})

    return Command(
        update={
            "messages": [
                ToolMessage(content=content, tool_call_id=tool_call_id),
            ],
        }
    )


# DALTON-HOOK(workspace-duplicate-guard): a create that would duplicate a
# workspace the user already holds never rides the standing approval — the
# middleware withholds it when the model proposes the call, and the duplicate
# is named on the card, so the user can just reuse it.
# --- DALTON-HOOK(workspace-folder-ready): begin ---
_FOLDER_NOT_READY_HINT = (
    "计算机启动后才会创建该文件夹；需要交接文件时，先放在 Home 下，并把路径写进派发说明"
)
_FOLDER_CHECK_NOTE = "复制或清理前先确认 folder_path 已存在且非空"


async def _folder_ready_result(
    manager: Any, user_id: str, workspace: dict
) -> dict:
    """``folder_ready``/``folder_path`` for a just-created workspace.

    A new row owns a folder name but no folder until the machine's next
    acquisition builds it, which left the Chief of Staff copying hand-off
    files into a directory that did not exist yet.

    The path a created folder must have is whatever the SANDBOX would use at
    bring-up — ``session.sandbox.layout.for_workspace(dir_name).setup_dirs[0]``,
    the exact expression ``PTCSandbox._ensure_workspace_dirs`` mkdirs — because
    a reconnect can repoint the sandbox's working dir away from the computer
    row's root, and a folder created at any other root is not the folder the
    attach will find. So whenever the folder is actually created (the new
    workspace is on THIS turn's Home computer and this worker holds the turn's
    cached session for it — a peek, never an acquire), the layout of that
    live sandbox is the one source for the path, for ``mkdir``, for ``test -d``
    and for the returned ``folder_path`` alike.

    Not creating (different computer, no cached session) answers the binding's
    ``root_dir``/``dir_name`` as a best-effort ``folder_path`` — display only,
    nothing was built there. A failed binding read never falls back to the
    global default root to build anything; it answers a default-derived path
    for display and ``folder_ready: False``. Even when creating, ``mkdir`` and
    a separate ``test -d`` must both exit 0 before ``folder_ready`` is true —
    a swallowed mkdir failure never reports ready. No case here may raise or
    fail the create, which already succeeded.
    """
    workspace_id = str(workspace.get("workspace_id") or "")
    dir_name = str(workspace.get("dir_name") or "").strip()
    computer_id = str(workspace.get("computer_id") or "").strip()

    result: dict = {"folder_ready": False, "folder_hint": _FOLDER_NOT_READY_HINT}

    binding = None
    try:
        binding = await manager.resolve_binding(workspace_id)
    except Exception as e:
        logger.warning(f"Could not resolve the folder path for {workspace_id}: {e}")
    if binding is None:
        # Display-only path from the same default math the file routes use;
        # never a root to build on.
        if dir_name:
            try:
                from src.server.app.workspace_files._shared import work_dir_for

                path = work_dir_for(workspace, manager=manager)
                if path:
                    result["folder_path"] = str(path)
            except Exception as e:
                logger.warning(
                    f"Could not compute the folder path for {workspace_id}: {e}"
                )
        return result

    root = str(getattr(binding, "root_dir", None) or "").rstrip("/")
    if root and dir_name:
        result["folder_path"] = f"{root}/{dir_name}"
    if not (root and dir_name and computer_id):
        return result

    try:
        from src.server.database.home_workspace import get_home_workspace_id
        from src.server.database.workspace import get_workspace as _get_workspace

        home = await _get_workspace(get_home_workspace_id(user_id))
        home_computer_id = str((home or {}).get("computer_id") or "")
    except Exception as e:
        logger.warning(f"Could not read Home's computer for user {user_id}: {e}")
        return result
    if home_computer_id != computer_id:
        return result

    # This worker's live session for the Home computer — a peek, never an
    # acquire: a machine with no cached session here is not running this turn.
    session = manager._cached_session(computer_id)
    sandbox = getattr(session, "sandbox", None) if session is not None else None
    if sandbox is None:
        return result

    try:
        # The one path bring-up would create: taken from the live sandbox's
        # own layout, which may differ from the binding root after a
        # reconnect repointed the sandbox's working dir.
        folder_path = str(
            sandbox.layout.for_workspace(dir_name).setup_dirs[0]
        )
    except Exception as e:
        logger.warning(
            f"Could not derive the sandbox folder path for {workspace_id}: {e}"
        )
        return result
    result["folder_path"] = folder_path

    import shlex

    def _exit_code(res: Any) -> int:
        try:
            return int((res or {}).get("exit_code"))
        except (TypeError, ValueError):
            return 1

    quoted = shlex.quote(folder_path)
    try:
        made = await sandbox.execute_bash_command(
            command=f"mkdir -p {quoted}", timeout=30
        )
        verified = await sandbox.execute_bash_command(
            command=f"test -d {quoted}", timeout=15
        )
        if _exit_code(made) == 0 and _exit_code(verified) == 0:
            return {
                "folder_ready": True,
                "folder_path": folder_path,
            }
        logger.warning(
            f"Folder for {workspace_id} was not confirmed at {folder_path}"
        )
    except Exception as e:
        logger.warning(f"Could not create the folder for {workspace_id}: {e}")
    return result
# --- DALTON-HOOK(workspace-folder-ready): end ---


async def workspaces_create(
    user_id: str,
    name: str | None,
    description: str | None,
    tool_call_id: str,
    preapproved: bool = False,
) -> Command:
    """Create a new workspace, once the user confirms unless ``preapproved``.

    ``preapproved`` is the record frozen when the model proposed the call, so
    on resume the user's answer to the card is what runs. The duplicate read
    below only writes the card's copy: whether to ask was already decided.
    """
    if not name:
        return error_command(
            "name is required for create action", tool_call_id
        )

    if not preapproved:
        holder, confirmed = await duplicate_workspace(user_id, name)
        payload = {
            "workspace_name": name,
            "workspace_description": description or "",
        }
        if holder is not None:
            payload["duplicate_workspace_name"] = holder.get("name")
            payload["duplicate_workspace_id"] = str(holder.get("workspace_id"))
        elif not confirmed:
            payload["duplicate_check_unavailable"] = True
        approved, response = hitl_confirm("create_workspace", payload)

        if not approved:
            # DALTON-HOOK(workspace-duplicate-guard): the decline names the
            # workspace to reuse instead.
            declined = "workspace creation"
            if holder is not None:
                declined = (
                    "workspace creation — they already have "
                    f"\"{holder.get('name')}\" (workspace_id "
                    f"{holder.get('workspace_id')}); use that workspace "
                    "instead of creating a duplicate"
                )
            return decline_routing_command(declined, response, tool_call_id)

    from src.server.database.workspace_names import (
        WorkspaceNameInvalid,
        WorkspaceNameTaken,
    )
    from src.server.services.computer_errors import ComputerPreparingError

    try:
        from src.server.services.workspace_manager import WorkspaceManager

        workspace_manager = WorkspaceManager.get_instance()
        workspace = await workspace_manager.create_workspace(
            user_id=user_id,
            name=name,
            description=description,
        )

        result = {
            "success": True,
            "workspace_id": str(workspace["workspace_id"]),
            "workspace_name": workspace["name"],
        }
        # --- DALTON-HOOK(workspace-folder-ready): begin ---
        # The folder is only built at the machine's next acquire, so say where
        # it will be and whether it is there now — the incident's hand-off
        # copied into a directory that did not exist yet.
        result.update(
            await _folder_ready_result(workspace_manager, user_id, workspace)
        )
        result["folder_note"] = _FOLDER_CHECK_NOTE
        # --- DALTON-HOOK(workspace-folder-ready): end ---
        if preapproved:
            # The card for it is drawn from this result.
            result["preapproved"] = True
            result["workspace_description"] = description or ""
        return success_command(result, tool_call_id)
    except WorkspaceNameTaken as e:
        if e.workspace_id is None:
            # The holder was renamed or deleted before it could be named.
            hint = "Try again, or create this one under a different name."
        else:
            hint = (
                f"Its workspace_id is {e.workspace_id}: use that workspace, "
                "or create this one under a different name."
            )
        return error_command(f"{e} {hint}", tool_call_id)
    except WorkspaceNameInvalid as e:
        return error_command(str(e), tool_call_id)
    except ComputerPreparingError:
        return error_command(
            "The user's computer is still being prepared, so no workspace can "
            "be created on it yet. Tell the user to try again once it is ready.",
            tool_call_id,
        )
    except Exception as e:
        logger.error(f"Failed to create workspace: {e}")
        return error_command("failed to create workspace", tool_call_id)


async def _workspaces_delete(
    user_id: str, workspace_id: str | None, tool_call_id: str
) -> Command:
    """Delete a workspace with HITL confirmation."""
    if not workspace_id:
        return error_command(
            "workspace_id is required for delete action", tool_call_id
        )

    from src.server.database.home_workspace import is_home_row
    from src.server.database.workspace import get_workspace

    workspace = await get_workspace(workspace_id)
    if not workspace or str(workspace.get("user_id")) != user_id:
        return error_command("workspace not found", tool_call_id)
    # Refused before the card, so the user is never asked to approve a delete
    # that cannot happen.
    if is_home_row(workspace):
        return error_command("Home cannot be deleted.", tool_call_id)

    # The card shows the name: an id alone cannot tell the user which
    # workspace they are approving the loss of.
    approved, _ = hitl_confirm(
        "delete_workspace",
        {"workspace_id": workspace_id, "workspace_name": workspace.get("name")},
    )

    if not approved:
        return decline_command(
            "User declined workspace deletion.", tool_call_id
        )

    from src.server.database.workspace import WorkspaceBusyError
    from src.server.services.workspace_manager import WorkspaceManager

    try:
        workspace_manager = WorkspaceManager.get_instance()
        await workspace_manager.delete_workspace(workspace_id)
        return success_command(
            {"success": True, "workspace_id": workspace_id},
            tool_call_id,
        )
    except (WorkspaceBusyError, ValueError) as e:
        # Refusals the user should hear: active work, or Home.
        return error_command(str(e), tool_call_id)
    except Exception as e:
        logger.error(f"Failed to delete workspace: {e}")
        return error_command("failed to delete workspace", tool_call_id)


async def threads_list(
    user_id: str,
    workspace_id: str | None,
    tool_call_id: str,
    own_thread_id: str | None = None,
) -> Command:
    """List threads, optionally filtered by workspace.

    A listing that shows another thread mid-run is marked for the loop guard,
    as a read of that thread is. The caller's own thread is always running,
    so it does not count.
    """
    from src.server.utils.pg_sanitize import normalize_uuid

    artifact = None
    try:
        if workspace_id:
            if err := await verify_workspace_owner(workspace_id, user_id, tool_call_id):
                return err

            from src.server.database.conversation.threads_read import get_workspace_threads

            threads, total = await get_workspace_threads(
                workspace_id=workspace_id, limit=20
            )
        else:
            from src.server.database.conversation.threads_read import get_threads_for_user

            # DALTON-HOOK(colleague-dispatch): pass channel="cos" so foreign workspaces are filtered in SQL before paging.
            threads, total = await get_threads_for_user(
                user_id=user_id, limit=20, channel="cos"
            )

        content = json.dumps(
            {
                "success": True,
                "threads": without_keys(threads, _OWNER_ID),
                "total": total,
            },
            default=str,
        )
        own = normalize_uuid(own_thread_id) if own_thread_id else None
        if any(
            is_live(row.get("current_status"))
            and normalize_uuid(row.get("conversation_thread_id")) != own
            for row in threads
        ):
            artifact = pending_read()
    except Exception as e:
        logger.error(f"Failed to list threads: {e}")
        content = json.dumps({"success": False, "error": "failed to list threads"})

    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=content, tool_call_id=tool_call_id, artifact=artifact
                ),
            ],
        }
    )


async def threads_stop(
    user_id: str,
    thread_id: str | None,
    own_thread_id: str | None,
    tool_call_id: str,
    run_id: str | None = None,
) -> Command:
    """Stop the turn ``run_id`` names on a thread, else whichever it is running.

    The same cancel as ``POST /cancel``, so the run ends through the finalize
    CAS and a hand-off with a report-back still reports that it was stopped.
    A run_id keeps a stop meant for a hand-off from ending a turn started in
    that thread after the hand-off finished.
    """
    from src.server.utils.pg_sanitize import normalize_uuid

    if not thread_id:
        return error_command("thread_id is required for stop", tool_call_id)
    thread_id = normalize_uuid(thread_id)
    if thread_id is None:
        return error_command("thread not found or not owned by user", tool_call_id)
    if thread_id == normalize_uuid(own_thread_id):
        return error_command(
            "That is this conversation; end your turn instead.", tool_call_id
        )
    # An empty run_id reads as omitted, as it does for POST /cancel.
    if run_id:
        run_id = normalize_uuid(run_id)
        if run_id is None:
            return error_command(
                "run_id is not a run id: pass the one delegate_to_analyst "
                "returned, or leave it out.",
                tool_call_id,
            )
    # DALTON-HOOK(workspace-sharing-settings): stop/delete thread requires ownership
    if err := await verify_thread_owner(
        thread_id, user_id, tool_call_id, need="write", owner_only=True
    ):
        return err

    from src.server.services.cancel_dispatch import cancel_workflow

    try:
        outcome = await cancel_workflow(
            thread_id, run_id=run_id or None, by_user=False
        )
    except Exception:
        # cancel_workflow logs the cause and raises it as a bare 500.
        return error_command("stop_failed", tool_call_id)
    if outcome["cancelled"]:
        # Said here, where the model decides what to do next: told only in the
        # prompt, it read the thread straight away and reported the stop twice.
        return success_command(
            {"success": True, "cancelled": True, "message": _STOPPING},
            tool_call_id,
        )
    # The stop ran and found nothing of that run to end; ``state`` says why.
    # The model named the thread to stop; echoing its id back invites it into
    # the reply.
    return success_command(
        {"success": True, **without_keys(outcome, {"thread_id", "returned_steers"})}, tool_call_id
    )


async def threads_delete(
    user_id: str, thread_id: str | None, tool_call_id: str
) -> Command:
    """Delete a thread with HITL confirmation."""
    if not thread_id:
        return error_command(
            "thread_id is required for delete action", tool_call_id
        )

    # DALTON-HOOK(workspace-sharing-settings): stop/delete thread requires ownership
    if err := await verify_thread_owner(
        thread_id, user_id, tool_call_id, need="write", owner_only=True
    ):
        return err

    approved, _ = hitl_confirm(
        "delete_thread",
        {"thread_id": thread_id},
    )

    if not approved:
        return decline_command(
            "User declined thread deletion.", tool_call_id
        )

    try:
        from src.server.database.conversation.threads_read import get_thread_by_id
        from src.server.services.commands.control import delete_thread_ending_commands
        from src.server.services.thread_mutation import (
            MutationConflict,
            MutationUnavailable,
        )

        # The row is gone after the delete, and the prune needs its workspace.
        try:
            thread_row = await get_thread_by_id(thread_id)
        except Exception:
            thread_row = None

        # Guarded delete, same fence as the HTTP endpoint (v4 2.4a): an
        # unfenced delete here would cascade away a live run's ledger rows
        # out from under a writer on any worker.
        try:
            await delete_thread_ending_commands(thread_id)
        except MutationConflict as e:
            detail = e.detail if isinstance(e.detail, dict) else {}
            return error_command(
                detail.get("message")
                or "Thread is busy (a run or mutation is in progress); "
                "stop it first, then retry the delete.",
                tool_call_id,
            )
        except MutationUnavailable:
            return error_command(
                "Thread deletion is temporarily unavailable; retry shortly.",
                tool_call_id,
            )

        # Invalidate thread existence cache (matches HTTP delete endpoint)
        try:
            from src.server.database.conversation.threads_write import thread_exists_key
            from src.utils.cache.redis_cache import get_cache_client
            cache = get_cache_client()
            if cache.enabled and cache.client:
                await cache.client.delete(thread_exists_key(thread_id))
        except Exception:
            pass

        # As the HTTP endpoint does: a bring-up skips a workspace whose live
        # threads look unchanged, so it would not prune this one's dirs.
        if thread_row:
            from src.server.services.workspace_manager import prune_thread_dirs_soon

            prune_thread_dirs_soon(str(thread_row["workspace_id"]))

        return success_command(
            {"success": True, "thread_id": thread_id},
            tool_call_id,
        )
    except Exception as e:
        logger.error(f"Failed to delete thread: {e}")
        return error_command("failed to delete thread", tool_call_id)


# --- DALTON-HOOK(workspace-sharing-settings): begin ---
def _is_home_workspace(workspace: dict) -> bool:
    """Whether ``workspace`` is the user's Home workspace."""
    if bool(workspace.get("is_home")):
        return True
    own_dir = str(workspace.get("dir_name") or "").strip()
    own_name = str(workspace.get("name") or "").strip()
    return own_dir == "Home" or (not own_dir and own_name == "Home")


async def _has_subdirectory(
    sandbox: object, work_dir: str, subdir_name: str, *, fail_open: bool
) -> bool:
    """True when ``<work_dir>/<subdir_name>`` already exists as a directory.

    A listing failure never refuses or rewrites the write: it logs a warning
    and answers ``fail_open`` — every caller passes True so the write proceeds
    on the ORIGINAL path rather than telling the model to strip a prefix the
    caller may genuinely mean."""
    try:
        entries = await sandbox.als_directory(work_dir)
    except Exception as e:
        logger.warning(
            f"Could not list directory {work_dir} when checking subdirectory {subdir_name!r}: {e}"
        )
        return fail_open
    for entry in entries or ():
        if (
            isinstance(entry, dict)
            and bool(entry.get("is_dir"))
            and str(entry.get("name") or "") == subdir_name
        ):
            return True
    return False


async def _sibling_folder_named(
    rel_path: str, workspace: dict, sandbox: object, work_dir: str
) -> str | None:
    """The path's first segment when ``workspace`` is Home, the first segment
    matches a LIVE (non-deleted) sibling workspace's folder on this computer,
    and Home does not already contain a subdirectory of that name."""
    if not _is_home_workspace(workspace):
        return None
    parts = rel_path.split("/", 1)
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        return None
    first = parts[0].strip()
    computer_id = workspace.get("computer_id")
    if not computer_id:
        logger.warning(
            f"Could not check sibling workspace folders for Home ({workspace.get('workspace_id')}): computer_id is empty"
        )
        return None
    try:
        from src.server.database.workspace import (
            get_live_workspace_folders_for_computer,
        )

        live_rows = await get_live_workspace_folders_for_computer(str(computer_id))
    except Exception as e:
        logger.warning(
            f"Could not read live workspace folders of computer {computer_id}: {e}"
        )
        return None
    own_id = str(workspace.get("workspace_id") or "").strip()
    own_dir = str(workspace.get("dir_name") or "Home").strip()
    matched_sibling: str | None = None
    for row in live_rows or ():
        if isinstance(row, dict):
            if own_id and str(row.get("workspace_id") or "") == own_id:
                continue
            folder = str(row.get("dir_name") or row.get("name") or "").strip()
        else:
            folder = str(row or "").strip()
        # Exact match: the sandbox is Linux and filenames are case-sensitive,
        # so "nvda/" in Home is an ordinary folder even beside an "NVDA"
        # workspace — same rule as the own-prefix and subdirectory checks.
        if folder and folder != own_dir and folder == first:
            matched_sibling = folder
            break
    if matched_sibling is None:
        return None
    if await _has_subdirectory(sandbox, work_dir, first, fail_open=True):
        return None
    return matched_sibling


async def workspace_files_list_cos(
    user_id: str,
    workspace_id: str | None,
    path: str | None,
    tool_call_id: str,
) -> Command:
    """List files in a workspace via Chief of Staff (`cos_access in ('ro', 'rw')`)."""
    if not workspace_id:
        return error_command("workspace_id is required for list_files", tool_call_id)
    if err := await verify_workspace_owner(
        workspace_id, user_id, tool_call_id, need="read"
    ):
        return err
    try:
        from src.server.app.workspace_files._shared import _is_non_owner_blocked_path
        from src.server.app.workspace_files.crud import _live_listing
        from src.server.database.workspace import get_workspace
        from src.server.models.workspace import served_from_mirror
        from src.server.services.persistence.file import FilePersistenceService
        from src.server.services.workspace_file_containment import (
            normalize_requested_path,
        )
        from src.server.services.workspace_files import (
            acquire_sandbox_to_change,
            owner_work_dir,
            previous_dir_names_of,
        )

        ws = await get_workspace(workspace_id)
        if not ws:
            return error_command("workspace not found", tool_call_id)
        work_dir = owner_work_dir(ws)
        previous_dirs = previous_dir_names_of(ws)
        normalized_path = normalize_requested_path(
            path or ".", work_dir, previous_dirs
        )
        if normalized_path and _is_non_owner_blocked_path(normalized_path):
            return success_command(
                {"success": True, "workspace_id": workspace_id, "files": []},
                tool_call_id,
            )

        if served_from_mirror(ws.get("status")):
            file_tree = await FilePersistenceService.get_file_tree(workspace_id)
            if normalized_path:
                file_tree = [
                    f
                    for f in file_tree
                    if f["path"].startswith(normalized_path + "/")
                    or f["path"] == normalized_path
                ]
            files = [
                f["path"]
                for f in file_tree
                if not _is_non_owner_blocked_path(f["path"])
            ]
        else:
            owner_id = str(ws.get("user_id") or user_id)
            async with acquire_sandbox_to_change(workspace_id, owner_id) as (
                sandbox,
                live_ws,
            ):
                files = await _live_listing(
                    sandbox,
                    live_ws,
                    path or ".",
                    "**/*",
                    False,
                    is_non_owner=True,
                )
        return success_command(
            {"success": True, "workspace_id": workspace_id, "files": files},
            tool_call_id,
        )
    except Exception as e:
        logger.error(f"Failed to list workspace files for CoS: {e}")
        return error_command("failed to list workspace files", tool_call_id)


async def workspace_file_read_cos(
    user_id: str,
    workspace_id: str | None,
    path: str | None,
    tool_call_id: str,
) -> Command:
    """Read a file from a workspace via Chief of Staff (`cos_access in ('ro', 'rw')`)."""
    if not workspace_id:
        return error_command("workspace_id is required for read_file", tool_call_id)
    if not path:
        return error_command("path is required for read_file", tool_call_id)
    if err := await verify_workspace_owner(
        workspace_id, user_id, tool_call_id, need="read"
    ):
        return err
    try:
        from fastapi import HTTPException
        from src.server.app.workspace_files._shared import (
            _decode_file_text,
            _is_binary,
            _is_non_owner_blocked_path,
            http_file_text,
        )
        from src.server.app.workspace_files.crud import _read_contained_target
        from src.server.database.workspace import get_workspace
        from src.server.models.workspace import served_from_mirror
        from src.server.services.persistence.file import FilePersistenceService
        from src.server.services.workspace_file_containment import (
            normalize_requested_path,
        )
        from src.server.services.workspace_files import (
            acquire_sandbox_to_change,
            owner_work_dir,
            previous_dir_names_of,
            to_client_path,
        )
        from src.server.utils.secret_redactor import (
            get_redactor,
            get_vault_secrets_for_redaction,
        )

        ws = await get_workspace(workspace_id)
        if not ws:
            return error_command("workspace not found", tool_call_id)
        work_dir = owner_work_dir(ws)
        previous_dirs = previous_dir_names_of(ws)
        normalized_path = normalize_requested_path(path, work_dir, previous_dirs)
        if not normalized_path or _is_non_owner_blocked_path(normalized_path):
            return error_command("file not found", tool_call_id)

        owner_id = str(ws.get("user_id") or user_id)
        if served_from_mirror(ws.get("status")):
            file_record = await FilePersistenceService.get_file_content(
                workspace_id, normalized_path
            )
            if not file_record:
                return error_command("file not found", tool_call_id)
            if file_record.get("is_binary"):
                return error_command("cannot read binary file as text", tool_call_id)

            text_content = await http_file_text(file_record, user_id=owner_id)
            out_path = normalized_path
        else:
            async with acquire_sandbox_to_change(workspace_id, owner_id) as (
                sandbox,
                live_ws,
            ):
                work_dir = owner_work_dir(live_ws)
                previous_dirs = previous_dir_names_of(live_ws)
                try:
                    normalized, raw_bytes = await _read_contained_target(
                        sandbox, path, work_dir, previous_dirs
                    )
                except HTTPException:
                    return error_command("file not found", tool_call_id)

            client_path = to_client_path(sandbox, normalized, work_dir)
            if _is_non_owner_blocked_path(client_path):
                return error_command("file not found", tool_call_id)
            if _is_binary(normalized):
                return error_command("cannot read binary file as text", tool_call_id)
            decoded = _decode_file_text(raw_bytes)
            if decoded is None:
                return error_command("cannot read binary file as text", tool_call_id)
            text_content = decoded
            out_path = client_path

        vault_secrets = await get_vault_secrets_for_redaction(owner_id)
        text_content = get_redactor().redact(text_content, vault_secrets=vault_secrets)
        return success_command(
            {
                "success": True,
                "workspace_id": workspace_id,
                "path": out_path,
                "content": text_content,
            },
            tool_call_id,
        )
    except Exception as e:
        logger.error(f"Failed to read workspace file for CoS: {e}")
        return error_command("failed to read workspace file", tool_call_id)


async def workspace_file_write_cos(
    user_id: str,
    workspace_id: str | None,
    path: str | None,
    content: str | None,
    tool_call_id: str,
) -> Command:
    """Write a text file in a workspace via Chief of Staff (`cos_access == 'rw'`)."""
    if not workspace_id:
        return error_command("workspace_id is required for write_file", tool_call_id)
    if not path:
        return error_command("path is required for write_file", tool_call_id)
    if content is None:
        return error_command("content is required for write_file", tool_call_id)
    if err := await verify_workspace_owner(
        workspace_id, user_id, tool_call_id, need="write"
    ):
        return err
    try:
        from src.server.app.workspace_files._shared import _is_non_owner_blocked_path
        from src.server.app.workspace_files.crud import MAX_WRITE_BYTES
        from src.server.database.workspace import get_workspace
        from src.server.models.workspace import served_from_mirror
        from src.server.services.workspace_file_containment import (
            normalize_requested_path,
        )
        from src.server.services.workspace_files import (
            acquire_sandbox_to_change,
            contained_target,
            owner_work_dir,
            previous_dir_names_of,
            to_client_path,
        )

        ws = await get_workspace(workspace_id)
        if not ws:
            return error_command("workspace not found", tool_call_id)
        if served_from_mirror(ws.get("status")):
            return error_command(
                f"Cannot write files — workspace is {ws.get('status')}. Wait for it to be running.",
                tool_call_id,
            )
        work_dir = owner_work_dir(ws)
        previous_dirs = previous_dir_names_of(ws)
        normalized_req_path = normalize_requested_path(path, work_dir, previous_dirs)
        if _is_non_owner_blocked_path(normalized_req_path):
            return error_command(
                "403 Forbidden: Cannot write system, hidden, or virtual files",
                tool_call_id,
            )

        content_bytes = content.encode("utf-8")
        if len(content_bytes) > MAX_WRITE_BYTES:
            return error_command("File content too large (max 10MB)", tool_call_id)

        owner_id = str(ws.get("user_id") or user_id)
        async with acquire_sandbox_to_change(workspace_id, owner_id) as (
            sandbox,
            live_ws,
        ):
            work_dir = owner_work_dir(live_ws)
            previous_dirs = previous_dir_names_of(live_ws)
            # DALTON-HOOK(workspace-sharing-settings): keep CoS writes inside the named workspace
            rel_path = normalize_requested_path(path, work_dir, previous_dirs)
            if _is_non_owner_blocked_path(rel_path):
                return error_command(
                    "403 Forbidden: Cannot write system, hidden, or virtual files",
                    tool_call_id,
                )
            own_dir = str(live_ws.get("dir_name") or "").strip()
            parts = rel_path.split("/", 1)
            if (
                own_dir
                and len(parts) == 2
                and parts[1].strip()
                and parts[0].strip() == own_dir
            ):
                if not await _has_subdirectory(
                    sandbox, work_dir, own_dir, fail_open=True
                ):
                    rest = parts[1].strip().lstrip("/")
                    return error_command(
                        f"路径应相对该工作区自己的文件夹，去掉开头的 `{own_dir}/`（如 '{rest}'）。",
                        tool_call_id,
                    )
            sibling = await _sibling_folder_named(
                rel_path, live_ws, sandbox, work_dir
            )
            if sibling is not None:
                # The incident's shape: "DASH/..." written into Home (or any
                # workspace but DASH) as a way to "put it in the workspace".
                # Refuse it naming the workspace-relative path, or the write
                # silently lands in the wrong workspace's folder.
                return error_command(
                    f"路径以 {sibling}/ 开头：那是同一电脑上另一个工作区的文件夹，"
                    f"不是工作区 {ws.get('name') or workspace_id} 内的路径，写入不会"
                    "落到目标工作区。请改用该工作区内的相对路径（如 'sources/x.md'），"
                    "或把 workspace_id 换成那个工作区。",
                    tool_call_id,
                )
            normalized = await contained_target(
                sandbox, rel_path, work_dir, previous_dirs
            )
            client_path = to_client_path(sandbox, normalized, work_dir)
            if _is_non_owner_blocked_path(client_path):
                return error_command(
                    "403 Forbidden: Cannot write system, hidden, or virtual files",
                    tool_call_id,
                )
            ok = await sandbox.awrite_file_text(normalized, content)
        if not ok:
            return error_command("Write failed", tool_call_id)
        return success_command(
            {
                "success": True,
                "workspace_id": workspace_id,
                "path": client_path,
                "size": len(content_bytes),
            },
            tool_call_id,
        )
    except Exception as e:
        logger.error(f"Failed to write workspace file for CoS: {e}")
        return error_command("failed to write workspace file", tool_call_id)


# One copy moves at most this many bytes: enough for any model workbook or
# deck, and safely under the mirror's 100 MB serving cap, so a copy never
# buffers more than the file routes already do.
_COS_COPY_MAX_BYTES = 50 * 1024 * 1024
_COS_ZIP_MEMBER_MAX_BYTES = 50 * 1024 * 1024
_COS_ZIP_TOTAL_MAX_BYTES = 100 * 1024 * 1024
_COS_ZIP_CONTAINER_EXTENSIONS = frozenset({".xlsx", ".xlsm", ".docx", ".pptx"})
_COS_LEGACY_OFFICE_EXTENSIONS = frozenset({".xls", ".doc", ".ppt"})
_COS_IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
)
_COS_COPY_ALLOWED_EXTENSIONS = (
    _COS_ZIP_CONTAINER_EXTENSIONS
    | _COS_LEGACY_OFFICE_EXTENSIONS
    | frozenset({".pdf"})
    | _COS_IMAGE_EXTENSIONS
)
_COS_NESTED_ARCHIVE_EXTENSIONS = frozenset(
    {
        ".zip",
        ".gz",
        ".tgz",
        ".bz2",
        ".tbz2",
        ".tar",
        ".7z",
        ".rar",
        ".xz",
        ".txz",
        ".zst",
    }
)
_COS_ARCHIVE_SIGNATURES = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"\x1f\x8b",
    b"BZh",
    b"7z\xbc\xaf\x27\x1c",
    b"Rar!\x1a\x07",
    b"\xfd7zXZ\x00",
    b"\x28\xb5\x2f\xfd",
)
_COS_COPY_UNSUPPORTED_ERROR = (
    "copy_file only supports binary Office (.xlsx, .xlsm, .docx, .pptx, .xls, "
    ".doc, .ppt), PDF (.pdf), and image (.png, .jpg, .jpeg, .gif, .webp, .bmp, "
    ".tiff) files; archives, pickle/database, audio/video, and text files are "
    "refused — read text files with read_file instead"
)
_COS_FORMAT_MISMATCH_ERROR = "文件内容和扩展名不一致，不能拷贝"
_COS_PDF_UNREADABLE_ERROR = "无法检查这个 PDF 的内容，不能拷贝"
_COS_EMBEDDED_OLE_ERROR = "文件里嵌有无法检查的对象，不能拷贝"
_COS_PDF_MAX_PAGES = 1000
# PDFs are scanned in a killable spawn child (src.dalton.heavy_compute): pypdf
# holds the GIL for seconds on a long document, so a thread would still stall
# this worker, and a crafted PDF can expand far past its file size. The child
# stops at this peak RSS and at the timeout (which includes waiting for a free
# child slot); either way the copy is refused. Office and image scans are
# bounded by the zip caps above and run in a thread, so they never queue
# behind a long publish audit for a child slot.
_COS_PDF_SCAN_TIMEOUT_S = 120.0
_COS_SCAN_MAX_RSS_BYTES = 1536 * 1024 * 1024
_COS_PDF_STREAM_TOTAL_MAX_BYTES = 100 * 1024 * 1024
_COS_SCAN_FAILED_ERROR = "文件内容检查没有完成，不能拷贝"
_COS_PDF_IMAGE_CODECS = frozenset({"/DCTDecode", "/JPXDecode", "/JBIG2Decode", "/CCITTFaxDecode"})
# Macro projects and ActiveX controls are OLE parts every .xlsm or form-bearing
# document carries; they are scanned as raw bytes. Embedded objects
# (embeddings/oleObject*.bin) wrap whole other files and are refused.
_COS_OOXML_OLE_RAW_SCAN_PARTS = re.compile(
    r"(xl|word|ppt)/(vbaProject\.bin|activeX/activeX\d+\.bin)", re.IGNORECASE
)
_COS_OOXML_MAX_RECURSION_DEPTH = 2
_OLE_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_SECRET_SCAN_ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be")


def _cos_read_channel_max_bytes(sandbox: Any) -> int:
    """The whole-file read ceiling of this workspace's sandbox channel.

    A cloud sandbox reads 64 MB; an SSH machine's exec keeps less output, so
    its honest ceiling is lower — a size refusal names the real one rather
    than claiming a uniform cap.
    """
    from src.server.services.workspace_file_containment import _exec_read_max

    return _exec_read_max(getattr(sandbox, "runtime", None))


def _is_allowed_copy_extension(file_path: str) -> bool:
    """Only extensions in ``_COS_COPY_ALLOWED_EXTENSIONS`` (Office, PDF, and
    common images) may use ``copy_file``; archives, pickle/database, audio/video,
    and any text format (including GBK/UTF-16 ``.csv``, ``.log``, ``.md``) are
    refused."""
    import os

    _, ext = os.path.splitext(file_path)
    return ext.lower() in _COS_COPY_ALLOWED_EXTENSIONS


def _has_archive_signature(buf: bytes) -> bool:
    return any(buf.startswith(sig) for sig in _COS_ARCHIVE_SIGNATURES)


def _is_ooxml_zip_bytes(buf: bytes) -> bool:
    """Return True if ``buf`` starts with ``PK\\x03\\x04`` and is a valid zip
    archive containing ``[Content_Types].xml``."""
    import io
    import zipfile

    if not buf.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(buf)) as zf:
            return "[Content_Types].xml" in zf.namelist()
    except Exception:
        return False


def _validate_copy_file_format(file_path: str, content: bytes) -> str | None:
    """Verify that the leading file bytes (and ``[Content_Types].xml`` for
    Office zip containers) match the file extension."""
    import os

    _, ext = os.path.splitext(file_path)
    ext_lower = ext.lower()
    if ext_lower == ".png":
        ok = content.startswith(b"\x89PNG\r\n\x1a\n")
    elif ext_lower in (".jpg", ".jpeg"):
        ok = content.startswith(b"\xff\xd8\xff")
    elif ext_lower == ".gif":
        ok = content.startswith((b"GIF87a", b"GIF89a"))
    elif ext_lower == ".webp":
        ok = (
            len(content) >= 12
            and content[:4] == b"RIFF"
            and content[8:12] == b"WEBP"
        )
    elif ext_lower == ".bmp":
        ok = content.startswith(b"BM")
    elif ext_lower == ".tiff":
        ok = content.startswith((b"II*\x00", b"MM\x00*"))
    elif ext_lower == ".pdf":
        ok = b"%PDF-" in content[:1024]
    elif ext_lower in _COS_LEGACY_OFFICE_EXTENSIONS:
        ok = content.startswith(_OLE_HEADER)
    elif ext_lower in _COS_ZIP_CONTAINER_EXTENSIONS:
        ok = _is_ooxml_zip_bytes(content)
    else:
        ok = False
    return None if ok else _COS_FORMAT_MISMATCH_ERROR


def _pdf_object_strings(obj: Any, _seen: set[int] | None = None) -> list[str]:
    """Every string in a PDF object's direct value, nested dictionaries and arrays included."""
    import pypdf.generic as g

    out: list[str] = []
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, (g.TextStringObject, g.ByteStringObject)):
            out.append(str(cur) if isinstance(cur, g.TextStringObject) else bytes(cur).decode("latin-1"))
        elif isinstance(cur, g.DictionaryObject):
            stack.extend(cur.get(k) for k in list(cur.keys()) if not isinstance(cur.raw_get(k), g.IndirectObject))
        elif isinstance(cur, g.ArrayObject):
            stack.extend(v for v in cur if not isinstance(v, g.IndirectObject))
    return out


def _scan_nested_file(data: bytes, vault_secrets: dict[str, str] | None, depth: int) -> str | None:
    """Scan a file embedded in a PDF stream by its own format; other archives are refused."""
    kind = _embedded_pdf_kind(data)
    if kind == "unreadable":
        return _COS_PDF_UNREADABLE_ERROR
    if kind == "pdf" or _is_ooxml_zip_bytes(data):
        if depth >= _COS_OOXML_MAX_RECURSION_DEPTH:
            return "文件内嵌层级超出安全上限，不能拷贝"
        name = "embedded.pdf" if kind == "pdf" else "embedded.xlsx"
        return _scan_copy_bytes_for_secrets(name, data, vault_secrets, depth + 1)
    if _is_real_archive(data):
        return "文件里内嵌压缩包，不能拷贝"
    return None


def _embedded_pdf_kind(data: bytes) -> str | None:
    """Whether stream bytes are a whole PDF, decided by opening them, not by keywords.

    A header within the first 1024 bytes (a direct copy's window) triggers a
    parse; bytes that open are a PDF and get scanned as one. Bytes that do
    not open stay ordinary content (their decoded bytes were already scanned):
    a page content stream may legally begin with a "%PDF-" comment.

    Known limit: an inner PDF broken past what pypdf's lenient parser accepts
    is not opened, so its compressed streams are not scanned.
    """
    import pypdf

    if b"%PDF-" not in data[:1024]:
        return None
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        # An encrypted PDF is a PDF even before it can be read; the recursive
        # scan tries the empty password and refuses it otherwise.
        if reader.is_encrypted:
            return "pdf"
        _ = len(reader.pages)
        return "pdf"
    except NotImplementedError:
        # pypdf raises this only after it has found /Encrypt and met an
        # encryption it does not support (an unknown /V, a non-Standard
        # filter): a real encrypted PDF that cannot be checked.
        return "unreadable"
    except Exception:
        return None


def _is_real_archive(data: bytes) -> bool:
    """A signature alone also matches raw pixels; the archive must open."""
    import bz2
    import lzma
    import zipfile
    import zlib

    if data.startswith(b"PK\x03\x04"):
        # A local file header is enough: a zip with no central directory
        # still carries deflated members, and the four-byte signature is as
        # specific as zstd's.
        return True
    if data.startswith(b"PK"):
        return zipfile.is_zipfile(io.BytesIO(data))

    def _opens(decoder: Any) -> bool:
        # A real stream yields output or reaches its end once enough input
        # arrives (a whole bzip2 block, a long gzip extra field); pixel bytes
        # that happen to match a signature error out or run dry first.
        for start in range(0, len(data), 65536):
            out = decoder.decompress(data[start : start + 65536], 64)
            if out or getattr(decoder, "eof", False):
                return True
        return False

    try:
        if data.startswith(b"\x1f\x8b\x08"):
            return _opens(zlib.decompressobj(16 + zlib.MAX_WBITS))
        if data.startswith(b"BZh"):
            return _opens(bz2.BZ2Decompressor())
        if data.startswith(b"\xfd7zXZ\x00"):
            return _opens(lzma.LZMADecompressor())
    except (zlib.error, OSError, EOFError, lzma.LZMAError, ValueError):
        return False
    return data.startswith((b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07", b"\x28\xb5\x2f\xfd"))


def _decode_pdf_text_filters(stream: Any, chain: list[Any]) -> bytes:
    """Undo the non-image filters in front of an image codec; an unknown one raises."""
    import pypdf.filters as pdf_filters

    decoders = {
        "/FlateDecode": pdf_filters.FlateDecode,
        "/Fl": pdf_filters.FlateDecode,
        "/ASCII85Decode": pdf_filters.ASCII85Decode,
        "/A85": pdf_filters.ASCII85Decode,
        "/ASCIIHexDecode": pdf_filters.ASCIIHexDecode,
        "/AHx": pdf_filters.ASCIIHexDecode,
        "/LZWDecode": pdf_filters.LZWDecode,
        "/LZW": pdf_filters.LZWDecode,
        "/RunLengthDecode": pdf_filters.RunLengthDecode,
        "/RL": pdf_filters.RunLengthDecode,
    }
    parms = stream.get("/DecodeParms")
    parms = parms.get_object() if hasattr(parms, "get_object") else parms
    data = stream._data
    for index, name in enumerate(chain):
        decoder = decoders.get(str(name))
        if decoder is None:
            raise ValueError(f"unsupported PDF filter {name}")
        parm = parms[index] if isinstance(parms, list) and index < len(parms) else None
        data = decoder.decode(data, parm)
    return data


def _scan_copy_bytes_for_secrets(
    file_path: str,
    content: bytes,
    vault_secrets: dict[str, str] | None,
    _depth: int = 0,
) -> str | None:
    """Validate file header magic against extension, then scan raw bytes,
    Office zip container members (with XML entity unescaping, recursive OOXML
    scanning up to 2 levels, embedded OLE rejection, and nested-archive
    rejection), and PDF text extracted via ``pypdf`` for any of the source
    owner's vault secret values (length >= ``MIN_REDACT_SECRET_LEN``).

    已知限制：使用自定义字体编码、没有 ToUnicode 映射的 PDF，pypdf 也提取不出原文，扫描覆盖不到。
    """
    import html
    import io
    import os
    import zipfile
    from src.server.utils.secret_redactor import MIN_REDACT_SECRET_LEN

    if format_err := _validate_copy_file_format(file_path, content):
        return format_err

    raw_secrets: list[str] = []
    needles: list[bytes] = []
    for raw_val in (vault_secrets or {}).values():
        if not isinstance(raw_val, str) or len(raw_val) < MIN_REDACT_SECRET_LEN:
            continue
        raw_secrets.append(raw_val)
        for enc in _SECRET_SCAN_ENCODINGS:
            encoded = raw_val.encode(enc)
            if encoded:
                needles.append(encoded)

    def _hits_secret_bytes(buf: bytes) -> bool:
        return bool(needles) and any(needle in buf for needle in needles)

    def _hits_secret_with_xml_unescape(buf: bytes) -> bool:
        if not raw_secrets:
            return False
        if _hits_secret_bytes(buf):
            return True
        decode_encodings = (
            ("utf-8", "utf-16-le", "utf-16-be")
            if b"\x00" in buf
            else ("utf-8",)
        )
        for enc in decode_encodings:
            text = buf.decode(enc, errors="ignore")
            if "&" in text:
                unescaped = html.unescape(text)
                if any(secret in unescaped for secret in raw_secrets):
                    return True
        return False

    if _hits_secret_bytes(content):
        return "文件里含有源用户的凭据，不能拷贝"

    _, ext = os.path.splitext(file_path)
    ext_lower = ext.lower()

    if ext_lower in _COS_ZIP_CONTAINER_EXTENSIONS:
        uncompressed_state = {"total": 0}

        def _scan_ooxml_container(zip_bytes: bytes, depth: int) -> str | None:
            try:
                with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        _, member_ext = os.path.splitext(info.filename)
                        member_ext_lower = member_ext.lower()
                        if member_ext_lower in _COS_NESTED_ARCHIVE_EXTENSIONS:
                            return "压缩容器内嵌压缩包，不能拷贝"
                        if info.file_size > _COS_ZIP_MEMBER_MAX_BYTES:
                            return "压缩容器单个成员超出安全上限，不能拷贝"
                        with zf.open(info) as member_fp:
                            member_bytes = member_fp.read(
                                _COS_ZIP_MEMBER_MAX_BYTES + 1
                            )
                        if len(member_bytes) > _COS_ZIP_MEMBER_MAX_BYTES:
                            return "压缩容器单个成员超出安全上限，不能拷贝"
                        uncompressed_state["total"] += len(member_bytes)
                        if uncompressed_state["total"] > _COS_ZIP_TOTAL_MAX_BYTES:
                            return "压缩容器解压总量超出安全上限，不能拷贝"
                        if member_bytes.startswith(_OLE_HEADER):
                            if not _COS_OOXML_OLE_RAW_SCAN_PARTS.fullmatch(info.filename):
                                return _COS_EMBEDDED_OLE_ERROR
                            if _hits_secret_bytes(member_bytes):
                                return "文件里含有源用户的凭据，不能拷贝"
                            continue
                        if _hits_secret_with_xml_unescape(member_bytes):
                            return "文件里含有源用户的凭据，不能拷贝"
                        if (
                            member_bytes.startswith(b"PK\x03\x04")
                            or member_ext_lower in _COS_ZIP_CONTAINER_EXTENSIONS
                        ):
                            if not _is_ooxml_zip_bytes(member_bytes):
                                return "压缩容器内嵌压缩包，不能拷贝"
                            if depth >= _COS_OOXML_MAX_RECURSION_DEPTH:
                                return "压缩容器内嵌层级超出安全上限，不能拷贝"
                            if nested_err := _scan_ooxml_container(
                                member_bytes, depth + 1
                            ):
                                return nested_err
                        elif _has_archive_signature(member_bytes):
                            return "压缩容器内嵌压缩包，不能拷贝"
            except Exception:
                return "压缩容器无法解压核验，不能拷贝"
            return None

        return _scan_ooxml_container(content, 0)

    # 已知限制：使用自定义字体编码、没有 ToUnicode 映射的 PDF，pypdf 也提取不出原文，扫描覆盖不到。
    if ext_lower == ".pdf":
        import pypdf
        import resource

        def _rss_exceeded() -> bool:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # ru_maxrss is KiB on Linux, bytes on macOS.
            peak_bytes = peak if sys.platform == "darwin" else peak * 1024
            return peak_bytes > _COS_SCAN_MAX_RSS_BYTES

        # Every stream object is decoded through pypdf, which applies the
        # object's own filter chain and the document's decryption, so
        # attachments, annotation bodies and form values the page text never
        # returns are scanned too, all within one running budget.
        try:
            reader = pypdf.PdfReader(io.BytesIO(content))
            if reader.is_encrypted and not reader.decrypt(""):
                return _COS_PDF_UNREADABLE_ERROR
            if len(reader.pages) > _COS_PDF_MAX_PAGES:
                return _COS_PDF_UNREADABLE_ERROR
            stream_budget = _COS_PDF_STREAM_TOTAL_MAX_BYTES
            # Every (number, generation) the cross-reference names, object
            # streams included; get_object(int) alone only sees generation 0.
            refs = {(num, gen) for gen, nums in reader.xref.items() for num in nums}
            refs.update((num, 0) for num in reader.xref_objStm)
            for num, gen in sorted(refs):
                if num == 0:
                    continue
                obj = pypdf.generic.IndirectObject(num, gen, reader).get_object()
                # Strings outside streams (document info, annotation
                # /Contents, form values) are ciphertext in an encrypted file;
                # pypdf hands them back decrypted here.
                for text in _pdf_object_strings(obj):
                    if raw_secrets and any(secret in text for secret in raw_secrets):
                        return "文件里含有源用户的凭据，不能拷贝"
                if not isinstance(obj, pypdf.generic.StreamObject):
                    continue
                filters = obj.get("/Filter")
                filters = filters.get_object() if isinstance(filters, pypdf.generic.IndirectObject) else filters
                chain = list(filters) if isinstance(filters, list) else ([filters] if filters else [])
                if chain and chain[-1] in _COS_PDF_IMAGE_CODECS:
                    # Every stream is scanned, whatever its dictionary says.
                    # An image codec (JPEG, JPEG 2000, JBIG2, CCITT) is left
                    # encoded: the text filters in front of it are undone and
                    # the codec's own bytes are scanned, so text hidden in a
                    # JPEG comment is found without decoding pixels.
                    data = _decode_pdf_text_filters(obj, chain[:-1])
                    stream_budget -= len(data)
                    if stream_budget < 0:
                        return _COS_PDF_UNREADABLE_ERROR
                    if _hits_secret_with_xml_unescape(data):
                        return "文件里含有源用户的凭据，不能拷贝"
                    if nested_err := _scan_nested_file(data, vault_secrets, _depth):
                        return nested_err
                    continue
                data = obj.get_data()
                stream_budget -= len(data)
                if stream_budget < 0:
                    return _COS_PDF_UNREADABLE_ERROR
                if _hits_secret_with_xml_unescape(data):
                    return "文件里含有源用户的凭据，不能拷贝"
                # A file carried inside (an attachment holding a workbook or
                # another PDF) is opened and scanned by its own rules.
                if nested_err := _scan_nested_file(data, vault_secrets, _depth):
                    return nested_err
                if _rss_exceeded():
                    return _COS_PDF_UNREADABLE_ERROR
            for page in reader.pages:
                page_text = page.extract_text() or ""
                if raw_secrets and any(secret in page_text for secret in raw_secrets):
                    return "文件里含有源用户的凭据，不能拷贝"
                if _rss_exceeded():
                    return _COS_PDF_UNREADABLE_ERROR
        except Exception:
            return _COS_PDF_UNREADABLE_ERROR

    return None


def _scan_copy_bytes_in_child(
    file_path: str,
    content: bytes,
    vault_secrets: dict[str, str] | None,
) -> str | None:
    """Entry point for the heavy-compute child; a memory blow-up refuses the copy."""
    try:
        return _scan_copy_bytes_for_secrets(file_path, content, vault_secrets)
    except MemoryError:
        return _COS_SCAN_FAILED_ERROR


async def workspace_file_copy_cos(
    user_id: str,
    workspace_id: str | None,
    path: str | None,
    dest_path: str | None,
    tool_call_id: str,
    dest_workspace_id: str | None = None,
) -> Command:
    """Copy an allowed binary file (Office, PDF, or image) out of a workspace into Home, or another of the user's own workspaces.

    ``read_file`` answers text only, so without this a shared workbook or PDF
    the Chief of Staff can see is one it cannot hand to the user. Only Office
    (``.xlsx``, ``.xlsm``, ``.docx``, ``.pptx``, ``.xls``, ``.doc``, ``.ppt``),
    PDF (``.pdf``), and image (``.png``, ``.jpg``, ``.jpeg``, ``.gif``,
    ``.webp``, ``.bmp``, ``.tiff``) files up to 50 MB are accepted; archives,
    pickle/database, audio/video, and text files are refused (text always goes
    through ``read_file``). The copy is a copy: the source is never touched,
    and what lands is a separate file.
    """
    if not workspace_id:
        return error_command("workspace_id is required for copy_file", tool_call_id)
    if not path:
        return error_command("path is required for copy_file", tool_call_id)
    if err := await verify_workspace_owner(
        workspace_id, user_id, tool_call_id, need="read"
    ):
        return err
    try:
        from fastapi import HTTPException
        from src.server.app.workspace_files._shared import (
            _is_non_owner_blocked_path,
            _to_client_path,
            http_file_bytes,
            owner_work_dir,
        )
        from src.server.app.workspace_files.crud import _read_contained_target
        from src.server.database.home_workspace import get_home_workspace_id
        from src.server.database.workspace import get_workspace
        from src.server.models.workspace import served_from_mirror
        from src.server.services.persistence.file import FilePersistenceService
        from src.server.services.workspace_file_containment import (
            normalize_requested_path,
        )
        from src.server.services.workspace_files import (
            acquire_sandbox_to_change,
            contained_target,
            previous_dir_names_of,
        )
        from src.server.utils.secret_redactor import (
            get_vault_secrets_for_redaction,
        )

        ws = await get_workspace(workspace_id)
        if not ws:
            return error_command("workspace not found", tool_call_id)
        work_dir = owner_work_dir(ws)
        previous_dirs = previous_dir_names_of(ws)
        normalized_path = normalize_requested_path(path, work_dir, previous_dirs)
        if not normalized_path or _is_non_owner_blocked_path(normalized_path):
            return error_command("file not found", tool_call_id)

        owner_id = str(ws.get("user_id") or user_id)
        if served_from_mirror(ws.get("status")):
            file_record = await FilePersistenceService.get_file_content(
                workspace_id, normalized_path
            )
            if not file_record:
                return error_command("file not found", tool_call_id)
            if not _is_allowed_copy_extension(normalized_path):
                return error_command(_COS_COPY_UNSUPPORTED_ERROR, tool_call_id)
            try:
                size = int(file_record.get("file_size") or 0)
            except (TypeError, ValueError):
                size = 0
            if size > _COS_COPY_MAX_BYTES:
                return error_command(
                    f"File is {size} bytes; the copy limit is "
                    f"{_COS_COPY_MAX_BYTES} bytes (50 MB).",
                    tool_call_id,
                )
            try:
                content = await http_file_bytes(file_record, user_id=owner_id)
            except HTTPException as e:
                return error_command(
                    f"cannot read the source file: {e.detail}", tool_call_id
                )
            if len(content) > _COS_COPY_MAX_BYTES:
                # file_size can be missing or 0 on the record; the bytes just
                # read are the authority then.
                return error_command(
                    f"File is {len(content)} bytes; the copy limit is "
                    f"{_COS_COPY_MAX_BYTES} bytes (50 MB).",
                    tool_call_id,
                )
            source_client_path = normalized_path
        else:
            if not _is_allowed_copy_extension(normalized_path):
                return error_command(_COS_COPY_UNSUPPORTED_ERROR, tool_call_id)
            async with acquire_sandbox_to_change(workspace_id, owner_id) as (
                sandbox,
                live_ws,
            ):
                work_dir = owner_work_dir(live_ws)
                previous_dirs = previous_dir_names_of(live_ws)
                try:
                    normalized, raw_bytes = await _read_contained_target(
                        sandbox, path, work_dir, previous_dirs
                    )
                except HTTPException as e:
                    if e.status_code != 413:
                        return error_command("file not found", tool_call_id)
                    limit = _cos_read_channel_max_bytes(sandbox)
                    return error_command(
                        f"file exceeds this workspace's read limit "
                        f"({limit} bytes, about {round(limit / (1024 * 1024))} MB "
                        "here); download it from the Files panel instead",
                        tool_call_id,
                    )
            source_client_path = _to_client_path(sandbox, normalized, work_dir)
            if _is_non_owner_blocked_path(source_client_path):
                return error_command("file not found", tool_call_id)
            if not _is_allowed_copy_extension(normalized):
                return error_command(_COS_COPY_UNSUPPORTED_ERROR, tool_call_id)
            if len(raw_bytes) > _COS_COPY_MAX_BYTES:
                return error_command(
                    f"File is {len(raw_bytes)} bytes; the copy limit is "
                    f"{_COS_COPY_MAX_BYTES} bytes (50 MB).",
                    tool_call_id,
                )
            content = raw_bytes

        vault_secrets = await get_vault_secrets_for_redaction(owner_id)
        if os.path.splitext(source_client_path)[1].lower() == ".pdf":
            from src.dalton.heavy_compute import (
                HeavyComputeError,
                HeavyComputeTimeout,
                run_in_heavy_subprocess,
            )

            try:
                secret_err = await run_in_heavy_subprocess(
                    _scan_copy_bytes_in_child,
                    source_client_path,
                    content,
                    vault_secrets,
                    timeout_s=_COS_PDF_SCAN_TIMEOUT_S,
                    label="copy_file_scan",
                )
            except (HeavyComputeError, HeavyComputeTimeout):
                secret_err = _COS_SCAN_FAILED_ERROR
        else:
            secret_err = await asyncio.to_thread(
                _scan_copy_bytes_in_child, source_client_path, content, vault_secrets
            )
        if secret_err:
            return error_command(secret_err, tool_call_id)

        # The destination is always the user's own: Home by default (where
        # send_message picks files up without a workspace_id), or a workspace
        # the copy names. A colleague's workspace is not a destination — a
        # write there goes through write_file, with its own access check.
        dest_id = (dest_workspace_id or "").strip() or get_home_workspace_id(user_id)
        dest_ws = await get_workspace(dest_id)
        if not dest_ws or str(dest_ws.get("user_id")) != user_id:
            return error_command(
                "destination must be Home or one of the user's own workspaces",
                tool_call_id,
            )
        dest_name = (dest_path or "").strip().lstrip("/") or (
            f"shared/{normalized_path.rsplit('/', 1)[-1]}"
        )
        if served_from_mirror(dest_ws.get("status")):
            return error_command(
                f"Cannot copy into {dest_ws.get('name') or dest_id} — it is "
                f"{dest_ws.get('status')}. Wait for it to be running.",
                tool_call_id,
            )
        async with acquire_sandbox_to_change(dest_id, user_id) as (
            dest_sandbox,
            dest_live_ws,
        ):
            dest_work_dir = owner_work_dir(dest_live_ws)
            dest_previous = previous_dir_names_of(dest_live_ws)
            dest_rel = normalize_requested_path(
                dest_name, dest_work_dir, dest_previous
            )
            if not dest_rel or _is_non_owner_blocked_path(dest_rel):
                return error_command(
                    "403 Forbidden: Cannot copy into system, hidden, or virtual paths",
                    tool_call_id,
                )
            sibling = await _sibling_folder_named(
                dest_rel, dest_live_ws, dest_sandbox, dest_work_dir
            )
            if sibling is not None:
                return error_command(
                    f"路径以 {sibling}/ 开头：那是同一电脑上另一个工作区的文件夹，"
                    "拷贝不会落到目标工作区。请改用工作区内的相对路径。",
                    tool_call_id,
                )
            dest_abs = await contained_target(
                dest_sandbox, dest_rel, dest_work_dir, dest_previous
            )
            ok = await dest_sandbox.aupload_file_bytes(dest_abs, content)
            dest_client_path = _to_client_path(
                dest_sandbox, dest_abs, dest_work_dir
            )
        if not ok:
            return error_command("Copy failed", tool_call_id)
        return success_command(
            {
                "success": True,
                "source": {"workspace_id": workspace_id, "path": source_client_path},
                "workspace_id": dest_id,
                "path": dest_client_path,
                "folder_path": dest_work_dir.rstrip("/"),
                "size": len(content),
            },
            tool_call_id,
        )
    except Exception as e:
        logger.error(f"Failed to copy workspace file for CoS: {e}")
        return error_command("failed to copy workspace file", tool_call_id)
# --- DALTON-HOOK(workspace-sharing-settings): end ---
