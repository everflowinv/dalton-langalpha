"""The workspace and thread actions behind the Chief of Staff's tools.

The tools themselves are in ``chief_of_staff``; a hand-off runs through
``dispatch``.
"""

import json
import logging

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

    A row names its folder only when the workspace is on Home's computer, the
    one whose folders the Chief of Staff reads.
    """
    try:
        from src.server.database.home_workspace import get_home_workspace_id
        from src.server.database.workspace import get_workspace, get_workspaces_for_user

        # Every workspace, since one left off the list is one the agent tells
        # the user does not exist. Pinned, then most recently used: the one a
        # hand-off is for is far likelier to be recent than early in the
        # user's custom order.
        workspaces, _ = await get_workspaces_for_user(
            user_id=user_id, limit=None, sort_by="activity"
        )
        from src.tools.secretary.activity import folder_on

        home = await get_workspace(get_home_workspace_id(user_id))
        computer_id = home.get("computer_id") if home else None
        workspaces = [
            {**ws, "dir_name": folder_on(ws, computer_id)} for ws in workspaces
        ]
        payload: dict[str, object] = {
            "success": True,
            "workspaces": [
                {field: ws.get(field) for field in _LISTED_WORKSPACE_FIELDS}
                for ws in workspaces
            ],
        }
        # --- DALTON-HOOK(workspace-sharing-settings): begin ---
        from src.dalton.settings import is_dalton_supabase_auth_enabled

        if is_dalton_supabase_auth_enabled():
            from src.dalton.sharing import list_colleague_cos_workspaces

            colleague_list = await list_colleague_cos_workspaces(user_id)
            payload["colleague_workspaces"] = colleague_list
            if colleague_list:
                # Said here, where the model reads the list it is about to
                # route by: promising a dispatch into one of these was the
                # incident, and dispatch refuses them.
                payload["colleague_workspaces_note"] = (
                    "同事共享的工作区当前只能读取（list_files / read_file；rw 时可 "
                    "write_file 写文件），delegate_to_analyst 不能向同事的工作区派发"
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

            threads, total = await get_threads_for_user(
                user_id=user_id, limit=20
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
        thread_id, user_id, tool_call_id, owner_only=True
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
        thread_id, user_id, tool_call_id, owner_only=True
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
        from src.server.database.conversation.threads_write import delete_thread
        from src.server.services.thread_mutation import (
            MutationConflict,
            MutationUnavailable,
            ThreadMutationRunner,
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
            async with ThreadMutationRunner.get_instance().exclusive(
                thread_id, "delete"
            ) as mutation:
                await delete_thread(thread_id, conn=mutation.conn)
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
def _strip_own_dir_prefix(rel_path: str, dir_name: str) -> str:
    """Drop a redundant leading ``<own folder>/`` from a workspace-relative path.

    A listing shows the workspace's folder name (``dir_name``), and a caller
    quoting it back ("DASH/sources/x.md" into the DASH workspace) means the
    file at ``sources/x.md`` — without this the write lands at
    ``DASH/DASH/sources/x.md`` and the workspace's analyst cannot find it."""
    if not dir_name:
        return rel_path
    stripped = rel_path
    parts = stripped.split("/", 1)
    while len(parts) == 2 and parts[1].strip() and (
        parts[0].strip().casefold() == dir_name.casefold()
    ):
        stripped = parts[1].strip().lstrip("/")
        parts = stripped.split("/", 1)
    return stripped


async def _sibling_folder_named(
    rel_path: str, workspace: dict
) -> str | None:
    """The path's first segment when it names ANOTHER workspace's folder on
    this computer — the shape of writing "DASH/..." into Home believing that
    creates the DASH workspace's folder. None when it names no sibling."""
    first = rel_path.split("/", 1)[0].strip()
    if not first:
        return None
    computer_id = workspace.get("computer_id")
    if not computer_id:
        return None
    try:
        from src.server.database.workspace import (
            get_workspace_dir_names_for_computer,
        )

        held = await get_workspace_dir_names_for_computer(str(computer_id))
    except Exception as e:
        logger.warning(
            f"Could not read workspace folders of computer {computer_id}: {e}"
        )
        return None
    own = str(workspace.get("dir_name") or "").strip().casefold()
    for name in held or ():
        if (
            name
            and name.casefold() == first.casefold()
            and name.casefold() != own
        ):
            return name
    return None


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
        from src.server.app.workspace_files._shared import (
            _acquire_sandbox_to_change,
            _is_non_owner_blocked_path,
            _normalize_requested_path,
            owner_work_dir,
            previous_dir_names_of,
        )
        from src.server.app.workspace_files.crud import _live_listing
        from src.server.database.workspace import get_workspace
        from src.server.models.workspace import served_from_mirror
        from src.server.services.persistence.file import FilePersistenceService

        ws = await get_workspace(workspace_id)
        if not ws:
            return error_command("workspace not found", tool_call_id)
        work_dir = owner_work_dir(ws)
        previous_dirs = previous_dir_names_of(ws)
        normalized_path = _normalize_requested_path(
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
            async with _acquire_sandbox_to_change(workspace_id, owner_id) as (
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
            _acquire_sandbox_to_change,
            _decode_file_text,
            _is_binary,
            _is_non_owner_blocked_path,
            _normalize_requested_path,
            _to_client_path,
            http_file_text,
            owner_work_dir,
            previous_dir_names_of,
        )
        from src.server.app.workspace_files.crud import _read_contained_target
        from src.server.database.workspace import get_workspace
        from src.server.models.workspace import served_from_mirror
        from src.server.services.persistence.file import FilePersistenceService
        from src.server.utils.secret_redactor import (
            get_redactor,
            get_vault_secrets_for_redaction,
        )

        ws = await get_workspace(workspace_id)
        if not ws:
            return error_command("workspace not found", tool_call_id)
        work_dir = owner_work_dir(ws)
        previous_dirs = previous_dir_names_of(ws)
        normalized_path = _normalize_requested_path(path, work_dir, previous_dirs)
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
            async with _acquire_sandbox_to_change(workspace_id, owner_id) as (
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

            client_path = _to_client_path(sandbox, normalized, work_dir)
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
        from src.server.app.workspace_files._shared import (
            _acquire_sandbox_to_change,
            _is_non_owner_blocked_path,
            _normalize_requested_path,
            _to_client_path,
            owner_work_dir,
            previous_dir_names_of,
        )
        from src.server.app.workspace_files.crud import (
            MAX_WRITE_BYTES,
            _contained_target,
        )
        from src.server.database.workspace import get_workspace
        from src.server.models.workspace import served_from_mirror

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
        normalized_req_path = _normalize_requested_path(path, work_dir, previous_dirs)
        if _is_non_owner_blocked_path(normalized_req_path):
            return error_command(
                "403 Forbidden: Cannot write system, hidden, or virtual files",
                tool_call_id,
            )

        content_bytes = content.encode("utf-8")
        if len(content_bytes) > MAX_WRITE_BYTES:
            return error_command("File content too large (max 10MB)", tool_call_id)

        owner_id = str(ws.get("user_id") or user_id)
        async with _acquire_sandbox_to_change(workspace_id, owner_id) as (
            sandbox,
            live_ws,
        ):
            work_dir = owner_work_dir(live_ws)
            previous_dirs = previous_dir_names_of(live_ws)
            # DALTON-HOOK(workspace-sharing-settings): keep CoS writes inside the named workspace
            rel_path = _strip_own_dir_prefix(
                _normalize_requested_path(path, work_dir, previous_dirs),
                str(live_ws.get("dir_name") or ""),
            )
            if _is_non_owner_blocked_path(rel_path):
                return error_command(
                    "403 Forbidden: Cannot write system, hidden, or virtual files",
                    tool_call_id,
                )
            sibling = await _sibling_folder_named(rel_path, live_ws)
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
            normalized = await _contained_target(
                sandbox, rel_path, work_dir, previous_dirs
            )
            client_path = _to_client_path(sandbox, normalized, work_dir)
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
# --- DALTON-HOOK(workspace-sharing-settings): end ---
