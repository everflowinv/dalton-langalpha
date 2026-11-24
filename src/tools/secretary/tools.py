"""The workspace and thread actions behind the Chief of Staff's tools.

The tools themselves are in ``chief_of_staff``; a hand-off runs through
``dispatch``.
"""

import asyncio
import io
import json
import logging
import os
import posixpath
import re
import shlex
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
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
                # DALTON-HOOK(cos-settings-tools): expose computer_id for multi-computer disambiguation
                if ws.get("computer_id"):
                    row["computer_id"] = str(ws["computer_id"])
            if machine := placement.machine(ws):
                row["machine"] = machine
            rows.append(row)
        payload: dict[str, object] = {"success": True, "workspaces": rows}
        # --- DALTON-HOOK(workspace-sharing-settings): begin ---
        # DALTON-HOOK(colleague-dispatch): adjust colleague_workspaces_note when colleague_dispatch is enabled.
        # DALTON-HOOK(colleague-access): gate on is_dalton_sharing_auth_enabled() and document UTF-8 text copy_file & analyst colleague_workspace.
        from src.dalton.settings import (
            is_colleague_dispatch_enabled,
            is_dalton_sharing_auth_enabled,
        )

        if is_dalton_sharing_auth_enabled():
            from src.dalton.sharing import list_colleague_cos_workspaces

            colleague_list = await list_colleague_cos_workspaces(user_id)
            payload["colleague_workspaces"] = colleague_list
            if colleague_list:
                if is_colleague_dispatch_enabled():
                    payload["colleague_workspaces_note"] = (
                        "同事的工作区：access（访问权限）为 rw（可读写）时，可用 "
                        "list_files、read_file、write_file、copy_file，也可用 "
                        "delegate_to_analyst 向该工作区的分析师派发任务；为 ro（只读）时，"
                        "仅可用 list_files、read_file 读取或用 copy_file 复制出来（派发任务仅限 "
                        "rw 工作区）。copy_file 可将 50 MB 以内的办公文档、PDF（便携式文档）或图片等"
                        "二进制文件，以及 5 MB 以内的 UTF-8（通用字符编码）文本文件原样复制到 "
                        "Home（个人主页）或用户自己的工作区；派发任务时若需分析师取用同事的工作区中的资料，"
                        '可在任务说明中提示其调用 colleague_workspace（含 colleague_workspace(action="copy_file")）。'
                    )
                else:
                    # Said here, where the model reads the list it is about to
                    # route by: promising a dispatch into one of these was the
                    # incident, and dispatch refuses them.
                    payload["colleague_workspaces_note"] = (
                        "同事的工作区：可用 list_files、read_file 读取，或用 copy_file 将 "
                        "50 MB 以内的办公文档、PDF（便携式文档）、图片等二进制文件及 5 MB 以内的 "
                        "UTF-8（通用字符编码）文本文件原样复制到 Home（个人主页）或用户自己的工作区；"
                        "access（访问权限）为 rw（可读写）时还可用 write_file 写文件。"
                        "当前部署下 delegate_to_analyst 不能向同事的工作区派发任务；"
                        '如需基于同事的工作区开展分析，请用 manage_workspaces(action="create") '
                        "在用户自己名下新建工作区并派发给其分析师，并在任务说明中提示分析师调用 "
                        'colleague_workspace（含 colleague_workspace(action="copy_file")）取用资料。'
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
        # DALTON-HOOK(cos-settings-tools): structured whitelisted error logging without raw exception text
        logger.warning(
            "folder_ready_error stage=%s workspace_id=%s computer_id=%s exc_type=%s",
            "resolve_binding",
            workspace_id,
            computer_id,
            type(e).__name__,
        )
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
                    "folder_ready_error stage=%s workspace_id=%s computer_id=%s exc_type=%s",
                    "work_dir_for",
                    workspace_id,
                    computer_id,
                    type(e).__name__,
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
        logger.warning(
            "folder_ready_error stage=%s workspace_id=%s computer_id=%s exc_type=%s",
            "home_lookup",
            workspace_id,
            computer_id,
            type(e).__name__,
        )
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
            "folder_ready_error stage=%s workspace_id=%s computer_id=%s exc_type=%s",
            "sandbox_layout",
            workspace_id,
            computer_id,
            type(e).__name__,
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
            "folder_ready_error stage=%s workspace_id=%s computer_id=%s",
            "verify_dir",
            workspace_id,
            computer_id,
        )
    except Exception as e:
        logger.warning(
            "folder_ready_error stage=%s workspace_id=%s computer_id=%s exc_type=%s",
            "mkdir_dir",
            workspace_id,
            computer_id,
            type(e).__name__,
        )
    return result
# --- DALTON-HOOK(workspace-folder-ready): end ---


async def workspaces_create(
    user_id: str,
    name: str | None,
    description: str | None,
    tool_call_id: str,
    preapproved: bool = False,
    # DALTON-HOOK(cos-settings-tools): optional target computer and frozen confirmation snapshot
    computer: str | None = None,
    snapshot: dict[str, Any] | None = None,
    # DALTON-HOOK(workspace-dedup): allow bypassing company dedup when user explicitly confirms
    allow_duplicate_company: bool = False,
    # DALTON-HOOK(workspace-dedup): optional coverage unit auto-registration fields
    coverage_kind: str | None = None,
    ticker: str | None = None,
    display_name: str | None = None,
    skip_coverage_suggestion: bool = False,
) -> Command:
    """Create a new workspace, once the user confirms unless ``preapproved``.

    ``preapproved`` and ``snapshot`` are frozen when the model proposed the
    call, so on resume the user's answer to the card runs strictly against the
    persisted snapshot without re-resolving the target computer.
    """
    if not name:
        return error_command(
            "name is required for create action", tool_call_id
        )

    # --- DALTON-HOOK(cos-settings-tools): begin ---
    # When invoked directly with an explicit ``computer`` outside the middleware,
    # resolve once before prompting; when ``snapshot`` came from thread state,
    # read only that snapshot (both before interrupt and on resume).
    from src.tools.secretary.approvals import build_create_workspace_snapshot

    if snapshot is None and computer is not None and computer.strip():
        snapshot = await build_create_workspace_snapshot(
            user_id,
            {
                "id": tool_call_id,
                "name": "manage_workspaces",
                "args": {
                    "action": "create",
                    "name": name,
                    "description": description,
                    "computer": computer,
                },
            },
        )
    if snapshot is not None and snapshot.get("precheck_error"):
        return error_command(
            str(snapshot["precheck_error"]),
            tool_call_id,
            code=str(snapshot.get("precheck_error_code") or "precheck_failed"),
        )
    # --- DALTON-HOOK(cos-settings-tools): end ---

    # --- DALTON-HOOK(workspace-dedup): begin ---
    from src.dalton.coverage.dedup import find_existing_company_workspace
    from src.dalton.coverage.units import is_workspace_coverage_registration_active

    raw_coverage_kind = str(coverage_kind or "").strip().lower()
    requested_coverage = raw_coverage_kind not in ("", "none", "other")
    coverage_active: bool | None = None
    if requested_coverage:
        coverage_active = await is_workspace_coverage_registration_active(user_id)
    wants_coverage = bool(requested_coverage and coverage_active)
    if not allow_duplicate_company:
        dup = None
        try:
            dup = await find_existing_company_workspace(
                user_id, name, channel="cos"
            )
        except Exception as exc:
            logger.warning(
                "Company workspace dedup check failed (%s); proceeding",
                type(exc).__name__,
            )
        if dup is not None and dup.matches:
            extra_fields: dict[str, Any] = {}
            suggestion = dup.suggestion_payload()
            if suggestion is not None:
                extra_fields["suggestion"] = suggestion
            return error_command(
                dup.message,
                tool_call_id,
                code="company_workspace_exists",
                message=dup.message,
                matches=dup.matches_payload(),
                **extra_fields,
            )
        if (
            dup is not None
            and dup.suggestion is not None
            and not wants_coverage
            and not skip_coverage_suggestion
        ):
            if coverage_active is None:
                coverage_active = await is_workspace_coverage_registration_active(user_id)
            if coverage_active:
                suggestion = dup.suggestion_payload()
                kind_label = (
                    "公司" if dup.suggestion.coverage_kind == "company" else "行业"
                )
                ticker_note = (
                    f"（代码 {dup.suggestion.ticker}）"
                    if dup.suggestion.ticker
                    else ""
                )
                msg = (
                    f"「{name}」识别为{kind_label}{ticker_note}，"
                    "请按 suggestion 带上 coverage_kind / ticker / display_name 重新新建以自动登记覆盖单元，"
                    "或在用户明确不要登记时传 skip_coverage_suggestion=True。"
                )
                return error_command(
                    msg,
                    tool_call_id,
                    code="coverage_suggestion",
                    message=msg,
                    suggestion=suggestion,
                )
    # --- DALTON-HOOK(workspace-dedup): end ---

    # --- DALTON-HOOK(cos-settings-tools): begin ---
    # Own machines always require per-call confirmation, even if preapproved was passed.
    if snapshot is not None and snapshot.get("computer_is_own_machine"):
        preapproved = False
    # --- DALTON-HOOK(cos-settings-tools): end ---

    if not preapproved:
        # --- DALTON-HOOK(cos-settings-tools): begin ---
        if snapshot is not None:
            dup_id = snapshot.get("duplicate_workspace_id")
            dup_name = snapshot.get("duplicate_workspace_name")
            holder = (
                {"workspace_id": dup_id, "name": dup_name}
                if dup_id is not None
                else None
            )
            confirmed = not bool(snapshot.get("duplicate_check_unavailable"))
            payload: dict[str, Any] = {
                "tool_call_id": tool_call_id,
                "workspace_name": snapshot.get("workspace_name") or name,
                "workspace_description": (
                    snapshot["workspace_description"]
                    if snapshot.get("workspace_description") is not None
                    else (description or "")
                ),
                "computer_id": snapshot.get("computer_id"),
                "computer_name": snapshot.get("computer_name"),
                "computer_is_own_machine": bool(snapshot.get("computer_is_own_machine")),
                "bootstrap_cloud_primary": bool(snapshot.get("bootstrap_cloud_primary")),
            }
        else:
            holder, confirmed = await duplicate_workspace(user_id, name)
            payload = {
                "tool_call_id": tool_call_id,
                "workspace_name": name,
                "workspace_description": description or "",
            }
        # --- DALTON-HOOK(cos-settings-tools): end ---
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

    # --- DALTON-HOOK(workspace-dedup): begin ---
    if allow_duplicate_company:
        matched_ids: list[str] = []
        try:
            dup_for_log = await find_existing_company_workspace(
                user_id, name, channel="cos"
            )
            if dup_for_log is not None and dup_for_log.matches:
                matched_ids = list(
                    dup_for_log.matched_workspace_ids
                    or [
                        m.workspace_id
                        for m in dup_for_log.matches
                        if m.workspace_id
                    ]
                )
        except Exception:
            matched_ids = []
        logger.info(
            "Skipping company workspace dedup check (allow_duplicate_company=True, user_id=%s, name=%r, matched_workspace_ids=%s)",
            user_id,
            name,
            matched_ids,
        )
    # --- DALTON-HOOK(workspace-dedup): end ---

    from src.server.database.workspace_names import (
        WorkspaceNameInvalid,
        WorkspaceNameTaken,
    )
    from src.server.services.computer_errors import (
        ApprovalScopeChangedError,  # DALTON-HOOK(cos-settings-tools)
        ComputerArchivingError,  # DALTON-HOOK(cos-settings-tools)
        ComputerNotFoundError,  # DALTON-HOOK(cos-settings-tools)
        ComputerPreparingError,
        ProviderNotOffered,  # DALTON-HOOK(cos-settings-tools)
    )

    # --- DALTON-HOOK(cos-settings-tools): begin ---
    snapshot_computer_id = (
        snapshot.get("computer_id") if snapshot is not None else None
    )
    snapshot_bootstrap = bool(
        snapshot is not None and snapshot.get("bootstrap_cloud_primary")
    )
    snapshot_computer_name = (
        snapshot.get("computer_name") if snapshot is not None else None
    )
    snapshot_computer_kind = (
        snapshot.get("computer_kind") if snapshot is not None else None
    )
    # Include preapproved metadata on failure results so the frontend renders a
    # failed CreateWorkspaceCard (e.g. scope_changed) on the standing-approval path.
    preapproved_meta: dict[str, Any] = {}
    if preapproved:
        preapproved_meta = {
            "preapproved": True,
            "workspace_name": name,
            "workspace_description": description or "",
        }
        if snapshot_computer_name:
            preapproved_meta["computer_name"] = snapshot_computer_name
        if snapshot_bootstrap:
            preapproved_meta["bootstrap_cloud_primary"] = True
    # --- DALTON-HOOK(cos-settings-tools): end ---

    try:
        from src.server.services.workspace_manager import WorkspaceManager

        workspace_manager = WorkspaceManager.get_instance()
        # --- DALTON-HOOK(cos-settings-tools): begin ---
        if snapshot is not None:
            create_kwargs: dict[str, Any] = {
                "user_id": user_id,
                "name": name,
                "description": description,
                "verify_snapshot": True,
            }
            if snapshot_computer_kind is not None:
                create_kwargs["expected_computer_kind"] = str(snapshot_computer_kind)
            if snapshot_bootstrap:
                create_kwargs["bootstrap_cloud_primary"] = True
            elif snapshot_computer_id is not None:
                create_kwargs["computer_id"] = str(snapshot_computer_id)
                if snapshot_computer_name is not None:
                    create_kwargs["expected_computer_name"] = str(
                        snapshot_computer_name
                    )
            else:
                return error_command(
                    "情况已变化，请重新发起",
                    tool_call_id,
                    code="scope_changed",
                    **preapproved_meta,
                )
            workspace = await workspace_manager.create_workspace(**create_kwargs)
        else:
            workspace = await workspace_manager.create_workspace(
                user_id=user_id,
                name=name,
                description=description,
            )
        # --- DALTON-HOOK(cos-settings-tools): end ---

        result = {
            "success": True,
            "workspace_id": str(workspace["workspace_id"]),
            "workspace_name": workspace["name"],
        }
        # --- DALTON-HOOK(cos-settings-tools): begin ---
        if snapshot is not None:
            if workspace.get("computer_id"):
                result["computer_id"] = str(workspace["computer_id"])
            if snapshot_computer_name:
                result["computer_name"] = snapshot_computer_name
            if snapshot_bootstrap:
                result["bootstrap_cloud_primary"] = True
        # --- DALTON-HOOK(cos-settings-tools): end ---
        # --- DALTON-HOOK(workspace-dedup): begin ---
        if wants_coverage:
            from src.dalton.coverage.units import auto_register_workspace_coverage_unit

            unit_row, unit_warning, unit_warning_detail = await auto_register_workspace_coverage_unit(
                user_id,
                workspace_id=str(workspace["workspace_id"]),
                workspace_name=str(workspace.get("name") or name),
                coverage_kind=coverage_kind,
                ticker=ticker,
                display_name=display_name,
            )
            if unit_row is not None:
                result["coverage_unit"] = unit_row
            if unit_warning is not None:
                result["coverage_unit_warning"] = unit_warning
            if unit_warning_detail is not None:
                result["coverage_unit_warning_detail"] = unit_warning_detail
        # --- DALTON-HOOK(workspace-dedup): end ---
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
        # DALTON-HOOK(cos-settings-tools): carry preapproved metadata on failure
        return error_command(f"{e} {hint}", tool_call_id, **preapproved_meta)
    except WorkspaceNameInvalid as e:
        # DALTON-HOOK(cos-settings-tools): carry preapproved metadata on failure
        return error_command(str(e), tool_call_id, **preapproved_meta)
    # --- DALTON-HOOK(cos-settings-tools): begin ---
    except ApprovalScopeChangedError:
        return error_command(
            "情况已变化，请重新发起",
            tool_call_id,
            code="scope_changed",
            **preapproved_meta,
        )
    except (ComputerNotFoundError, ComputerArchivingError):
        if snapshot is not None:
            return error_command(
                "情况已变化，请重新发起",
                tool_call_id,
                code="scope_changed",
                **preapproved_meta,
            )
        return error_command(
            "failed to create workspace", tool_call_id, **preapproved_meta
        )
    except ProviderNotOffered:
        return error_command(
            "当前部署默认提供商不支持自动创建托管云端主电脑，请指定已有电脑或联系管理员配置托管云端提供商。",
            tool_call_id,
            code="provider_not_offered",
            **preapproved_meta,
        )
    # --- DALTON-HOOK(cos-settings-tools): end ---
    except ComputerPreparingError:
        if snapshot is not None:
            # DALTON-HOOK(cos-settings-tools): snapshot target changed to preparing state
            return error_command(
                "情况已变化，请重新发起",
                tool_call_id,
                code="scope_changed",
                **preapproved_meta,
            )
        return error_command(
            "The user's computer is still being prepared, so no workspace can "
            "be created on it yet. Tell the user to try again once it is ready.",
            tool_call_id,
            **preapproved_meta,
        )
    except Exception as e:
        # DALTON-HOOK(cos-settings-tools): log only whitelisted fields without raw exception text
        logger.error(
            "workspace_create_failed computer_id=%s exc_type=%s",
            snapshot_computer_id or "",
            type(e).__name__,
        )
        return error_command(
            "failed to create workspace", tool_call_id, **preapproved_meta
        )


# --- DALTON-HOOK(cos-settings-tools): begin ---
def _http_error_message(exc: Any) -> str:
    """Human-readable refusal text from an ``HTTPException``."""
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("detail") or detail)
    if detail:
        return str(detail)
    return str(exc)


async def workspaces_update(
    user_id: str,
    workspace_id: str | None,
    name: str | None,
    description: str | None,
    tool_call_id: str,
) -> Command:
    """Update a workspace's name and/or description, matching PUT /workspaces/{id}."""
    if not workspace_id:
        return error_command(
            "workspace_id is required for update action", tool_call_id
        )
    if name is None and description is None:
        return error_command(
            "name or description is required for update action", tool_call_id
        )

    from fastapi import HTTPException
    from pydantic import ValidationError
    from src.server.database.home_workspace import is_reserved_row
    from src.server.database.workspace import (
        get_workspace,
        update_workspace as db_update_workspace,
    )
    from src.server.database.workspace_folders import WorkspaceFolderMoving
    from src.server.database.workspace_names import (
        WorkspaceNameInvalid,
        WorkspaceNameTaken,
    )
    from src.server.models.workspace import WorkspaceUpdate
    from src.server.services.shared_workspaces.access import workspace_access
    from src.server.services.shared_workspaces.feed import publish_workspace_changed

    try:
        validated = WorkspaceUpdate(name=name, description=description)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = ".".join(str(part) for part in err.get("loc") or ())
        msg = err.get("msg") or str(exc)
        return error_command(f"{loc}: {msg}" if loc else msg, tool_call_id)

    try:
        workspace = await get_workspace(workspace_id)
        access = await workspace_access(workspace, user_id, manage=True)
    except HTTPException as exc:
        return error_command(_http_error_message(exc), tool_call_id)
    except Exception as e:
        logger.error(
            "workspace_update_failed stage=%s workspace_id=%s exc_type=%s",
            "verify_access",
            workspace_id,
            type(e).__name__,
        )
        return error_command("failed to update workspace", tool_call_id)

    reserved = is_reserved_row(workspace)
    if (
        reserved
        and validated.name is not None
        and validated.name != workspace.get("name")
    ):
        return error_command("Home can't be renamed.", tool_call_id)
    effective_name = (
        None if (reserved and validated.name == workspace.get("name")) else validated.name
    )

    try:
        updated = await db_update_workspace(
            workspace_id=workspace_id,
            name=effective_name,
            description=validated.description,
        )
        if not updated:
            return error_command("Workspace not found", tool_call_id)
        if access.is_shared and access.org_id:
            await publish_workspace_changed(access.org_id, workspace_id, "updated")
        return success_command(
            {
                "success": True,
                "workspace_id": str(updated["workspace_id"]),
                "name": updated.get("name"),
                "description": updated.get("description"),
                "dir_name": updated.get("dir_name"),
            },
            tool_call_id,
        )
    except WorkspaceNameTaken as e:
        if e.workspace_id is None:
            hint = "Try again with a different name."
        else:
            hint = (
                f"Its workspace_id is {e.workspace_id}: choose a different name."
            )
        return error_command(f"{e} {hint}", tool_call_id)
    except WorkspaceNameInvalid as e:
        return error_command(str(e), tool_call_id)
    except WorkspaceFolderMoving:
        return error_command(
            "This workspace's folder is being moved. Try renaming it again in a moment.",
            tool_call_id,
        )
    except Exception as e:
        logger.error(
            "workspace_update_failed stage=%s workspace_id=%s exc_type=%s",
            "db_update",
            workspace_id,
            type(e).__name__,
        )
        return error_command("failed to update workspace", tool_call_id)
# --- DALTON-HOOK(cos-settings-tools): end ---


async def _workspaces_delete(
    user_id: str, workspace_id: str | None, tool_call_id: str
) -> Command:
    """Delete a workspace with HITL confirmation."""
    if not workspace_id:
        return error_command(
            "workspace_id is required for delete action", tool_call_id
        )

    from src.server.database.home_workspace import is_reserved_row
    from src.server.database.workspace import get_workspace

    workspace = await get_workspace(workspace_id)
    if not workspace or str(workspace.get("user_id")) != user_id:
        return error_command("workspace not found", tool_call_id)
    # Refused before the card, so the user is never asked to approve a delete
    # that cannot happen.
    if is_reserved_row(workspace):
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
        # DALTON-HOOK(computer-cli-tokens-off): 跟上上游 cancel_workflow 的 by: Stopper 签名
        outcome = await cancel_workflow(
            thread_id, run_id=run_id or None, by="agent"
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
    *,
    viewer_user_ids: Sequence[str] | None = None,
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
            _is_non_owner_blocked_path,
            _is_protected_workspace_path,
        )
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
        ws_owner_id = str(ws.get("user_id") or "")
        viewers = tuple(str(u) for u in (viewer_user_ids or (user_id,)))
        is_non_owner = not ws_owner_id or any(u != ws_owner_id for u in viewers)
        path_blocked = (
            _is_non_owner_blocked_path if is_non_owner else _is_protected_workspace_path
        )
        work_dir = owner_work_dir(ws)
        previous_dirs = previous_dir_names_of(ws)
        normalized_path = normalize_requested_path(
            path or ".", work_dir, previous_dirs
        )
        if normalized_path and path_blocked(normalized_path):
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
                if not path_blocked(f["path"])
            ]
        else:
            owner_id = ws_owner_id or user_id
            from src.server.app.workspace_files._shared import RoutePaths

            async with acquire_sandbox_to_change(workspace_id, owner_id) as (
                sandbox,
                live_ws,
            ):
                files = await _live_listing(
                    sandbox,
                    RoutePaths.of(live_ws, owner_id),
                    path or ".",
                    "**/*",
                    False,
                    is_non_owner=is_non_owner,
                )
            if not is_non_owner:
                files = [f for f in files if not path_blocked(f)]
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
    *,
    viewer_user_ids: Sequence[str] | None = None,
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
            _is_protected_workspace_path,
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
        ws_owner_id = str(ws.get("user_id") or "")
        viewers = tuple(str(u) for u in (viewer_user_ids or (user_id,)))
        is_non_owner = not ws_owner_id or any(u != ws_owner_id for u in viewers)
        path_blocked = (
            _is_non_owner_blocked_path if is_non_owner else _is_protected_workspace_path
        )
        work_dir = owner_work_dir(ws)
        previous_dirs = previous_dir_names_of(ws)
        normalized_path = normalize_requested_path(path, work_dir, previous_dirs)
        if not normalized_path or path_blocked(normalized_path):
            return error_command("file not found", tool_call_id)

        owner_id = ws_owner_id or user_id
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
                    contained = await _read_contained_target(
                        sandbox, path, work_dir, previous_dirs
                    )
                    # Upstream returns a ContainedFile (canonical path, bytes, stamp).
                    normalized, raw_bytes = contained.canonical, contained.content
                except HTTPException:
                    return error_command("file not found", tool_call_id)

            client_path = to_client_path(sandbox, normalized, work_dir)
            if path_blocked(client_path):
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
            _is_non_owner_blocked_path,
            _is_protected_workspace_path,
        )
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
        ws_owner_id = str(ws.get("user_id") or "")
        is_non_owner = not ws_owner_id or ws_owner_id != str(user_id)
        path_blocked = (
            _is_non_owner_blocked_path if is_non_owner else _is_protected_workspace_path
        )
        work_dir = owner_work_dir(ws)
        previous_dirs = previous_dir_names_of(ws)
        normalized_req_path = normalize_requested_path(path, work_dir, previous_dirs)
        if path_blocked(normalized_req_path):
            return error_command(
                "403 Forbidden: Cannot write system, hidden, or virtual files",
                tool_call_id,
            )

        content_bytes = content.encode("utf-8")
        if len(content_bytes) > MAX_WRITE_BYTES:
            return error_command("File content too large (max 10MB)", tool_call_id)

        owner_id = ws_owner_id or user_id
        async with acquire_sandbox_to_change(workspace_id, owner_id) as (
            sandbox,
            live_ws,
        ):
            work_dir = owner_work_dir(live_ws)
            previous_dirs = previous_dir_names_of(live_ws)
            # DALTON-HOOK(workspace-sharing-settings): keep CoS writes inside the named workspace
            rel_path = normalize_requested_path(path, work_dir, previous_dirs)
            if path_blocked(rel_path):
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
            if path_blocked(client_path):
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
# DALTON-HOOK(colleague-access): UTF-8 whitelisted text copy cap (5 MB) and allowed extensions
_COS_COPY_TEXT_MAX_BYTES = 5 * 1024 * 1024
_COS_ZIP_MEMBER_MAX_BYTES = 50 * 1024 * 1024
_COS_ZIP_TOTAL_MAX_BYTES = 100 * 1024 * 1024
_COS_ZIP_MAX_MEMBERS = 2000
_COS_ZIP_CHUNK_BYTES = 64 * 1024
# DALTON-HOOK(colleague-access): refuse macro-enabled Office formats (new and legacy OLE) and scan OOXML parts with the text secret redactor
_COS_MACRO_OFFICE_EXTENSIONS = frozenset({".xlsm", ".docm", ".pptm", ".xlam"})
_COS_MACRO_OFFICE_REFUSED_ERROR = (
    "带宏的 Office 文件（.xlsm、.docm、.pptm、.xlam 或含 VBA 宏的 .xls/.doc/.xlsx/.docx/.pptx）"
    "不支持通过 copy_file 复制，因为宏项目可能包含无法安全核验的代码或凭据；"
    "请另存为无宏格式（.xlsx、.docx、.pptx）后再复制。"
)
_COS_LEGACY_PPT_EXTENSIONS = frozenset({".ppt"})
_COS_LEGACY_PPT_REFUSED_ERROR = (
    "老格式 PowerPoint（.ppt）可能内嵌宏，无法检查。请让对方另存为 .pptx 后再复制。"
)
_COS_ZIP_CONTAINER_EXTENSIONS = frozenset({".xlsx", ".docx", ".pptx"})
_COS_OOXML_XML_EXTENSIONS = frozenset({".xml", ".rels"})
_COS_LEGACY_OFFICE_EXTENSIONS = frozenset({".xls", ".doc"})
_OLE_VBA_DIR_NAMES = frozenset(
    {"_vba_project", "_vba_project_cur", "vba", "macros", "vbaproject"}
)
_OLE_VBA_UTF16LE_RE = re.compile(
    b"|".join(
        b"".join(
            (
                b"[" + ch.lower().encode("ascii") + ch.upper().encode("ascii") + b"]\x00"
                if ch.isalpha()
                else re.escape(ch.encode("ascii")) + b"\x00"
            )
            for ch in name
        )
        + b"\x00\x00"
        for name in ("_VBA_PROJECT", "_VBA_PROJECT_CUR", "VBA", "Macros", "VBAProject")
    )
)
_COS_IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
)
_COS_TEXT_COPY_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".py",
        ".sql",
        ".html",
        ".xml",
        ".log",
    }
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
    "copy_file supports binary Office (.xlsx, .docx, .pptx, .xls, "
    ".doc), PDF (.pdf), and image (.png, .jpg, .jpeg, .gif, .webp, "
    ".bmp, .tiff) files up to 50 MB, and UTF-8 text files (.md, .markdown, "
    ".txt, .csv, .tsv, .json, .jsonl, .yaml, .yml, .py, .sql, .html, .xml, "
    ".log) up to 5 MB. Use read_file to read other text files."
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
# DALTON-HOOK(colleague-access): refuse VBA macro projects inside OOXML containers; keep ActiveX raw-scanned.
_COS_OOXML_VBA_MACRO_PART = re.compile(
    r"(xl|word|ppt)/vbaProject\.bin", re.IGNORECASE
)
_COS_OOXML_OLE_RAW_SCAN_PARTS = re.compile(
    r"(xl|word|ppt)/activeX/activeX\d+\.bin", re.IGNORECASE
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


# DALTON-HOOK(colleague-access): support both binary Office/PDF/image (<=50 MB) and whitelisted strict UTF-8 text (<=5 MB) copy
def _is_macro_office_extension(file_path: str) -> bool:
    _, ext = os.path.splitext(file_path)
    return ext.lower() in _COS_MACRO_OFFICE_EXTENSIONS


def _is_legacy_ppt_extension(file_path: str) -> bool:
    _, ext = os.path.splitext(file_path)
    return ext.lower() in _COS_LEGACY_PPT_EXTENSIONS


def _is_allowed_binary_copy_extension(file_path: str) -> bool:
    _, ext = os.path.splitext(file_path)
    return ext.lower() in _COS_COPY_ALLOWED_EXTENSIONS


def _is_allowed_text_copy_extension(file_path: str) -> bool:
    _, ext = os.path.splitext(file_path)
    return ext.lower() in _COS_TEXT_COPY_EXTENSIONS


def _is_allowed_copy_extension(file_path: str) -> bool:
    """Allow binary Office/PDF/image files (<=50 MB) and whitelisted UTF-8 text files (<=5 MB)."""
    return _is_allowed_binary_copy_extension(file_path) or _is_allowed_text_copy_extension(file_path)


def _copy_max_bytes_for_path(file_path: str) -> tuple[int, str]:
    """Return ``(max_bytes, human_label)`` for a copyable file path."""
    if _is_allowed_text_copy_extension(file_path):
        return _COS_COPY_TEXT_MAX_BYTES, "5 MB"
    return _COS_COPY_MAX_BYTES, "50 MB"


def _scan_copy_text_for_secrets(
    content: bytes,
    vault_secrets: dict[str, str] | None,
) -> str | None:
    """Validate strict UTF-8 text (<=5 MB, no NUL) and run dual secret scans.

    Rejects:
    - files larger than 5 MB;
    - files containing NUL bytes (catches UTF-16 with ASCII or binary masquerading as text);
    - files that fail strict ``utf-8-sig`` decoding (catches GBK / UTF-16 BOM / malformed bytes);
    - decoded text flagged by ``get_redactor().redact(..., vault_secrets=vault_secrets)``;
    - raw bytes containing any source-owner vault secret (len >= ``MIN_REDACT_SECRET_LEN``)
      encoded in ``utf-8``, ``utf-16-le``, or ``utf-16-be``.
    """
    from src.server.utils.secret_redactor import MIN_REDACT_SECRET_LEN, get_redactor

    if len(content) > _COS_COPY_TEXT_MAX_BYTES:
        return (
            f"File is {len(content)} bytes; the UTF-8 text copy limit is "
            f"{_COS_COPY_TEXT_MAX_BYTES} bytes (5 MB)."
        )
    if b"\x00" in content:
        return "copy_file 仅支持 UTF-8（通用字符编码）文本文件；该文件含 NUL（空字节）或采用其他编码，请改用 read_file 读取文本。"
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "copy_file 仅支持 UTF-8（通用字符编码）文本文件；该文件采用其他编码，请改用 read_file 读取文本。"

    if get_redactor().redact(decoded, vault_secrets=vault_secrets) != decoded:
        return "文件含有密钥或该工作区主人的凭据，无法原样复制；请改用 read_file 读取脱敏后的内容。"

    for raw_val in (vault_secrets or {}).values():
        if not isinstance(raw_val, str) or len(raw_val) < MIN_REDACT_SECRET_LEN:
            continue
        for enc in _SECRET_SCAN_ENCODINGS:
            encoded = raw_val.encode(enc)
            if encoded and encoded in content:
                return "文件含有该工作区主人的凭据，无法原样复制；请改用 read_file 读取脱敏后的内容。"

    return None


def _has_archive_signature(buf: bytes) -> bool:
    return any(buf.startswith(sig) for sig in _COS_ARCHIVE_SIGNATURES)


def _count_zip_central_entries(buf: bytes, limit: int) -> int | None:
    """Count central directory entries the way ``ZipFile`` walks them, stopping
    once ``limit`` is passed, so an end record that understates the count is
    caught before ``ZipFile`` builds every entry. ``None`` for a malformed
    directory. Locating the directory reuses ``zipfile._EndRecData``, the same
    reader ``ZipFile`` uses (ZIP64 and prefixed archives included)."""
    import struct
    import zipfile

    try:
        endrec = zipfile._EndRecData(io.BytesIO(buf))
    except (zipfile.BadZipFile, OSError, struct.error):
        return None
    if not endrec:
        return None
    size_cd = endrec[zipfile._ECD_SIZE]
    offset_cd = endrec[zipfile._ECD_OFFSET]
    # ZipFile's own arithmetic. For ZIP64, _ECD_LOCATION points at the ZIP64 end
    # record from 3.13.10 / 3.14.1 on and at the classic end record before, which
    # puts the directory 76 bytes later; try that start too, so either runtime
    # locates the same directory.
    concat = endrec[zipfile._ECD_LOCATION] - size_cd - offset_cd
    starts = [offset_cd + concat]
    if endrec[zipfile._ECD_SIGNATURE] == zipfile.stringEndArchive64:
        starts.append(starts[0] - zipfile.sizeEndCentDir64 - zipfile.sizeEndCentDir64Locator)
    for start in starts:
        end = start + size_cd
        if start < 0 or end > len(buf) or buf[start : start + 4] != zipfile.stringCentralDir:
            continue
        return _walk_central_entries(buf, start, end, limit)
    return None


def _walk_central_entries(buf: bytes, pos: int, end: int, limit: int) -> int | None:
    import struct
    import zipfile

    count = 0
    while pos < end:
        if pos + zipfile.sizeCentralDir > end or buf[pos : pos + 4] != zipfile.stringCentralDir:
            return None
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", buf, pos + 28)
        pos += zipfile.sizeCentralDir + name_len + extra_len + comment_len
        if pos > end:
            # This entry's name, extra or comment runs past the directory.
            return None
        count += 1
        if count > limit:
            return count
    return count


def _read_zip_eocd_member_count(buf: bytes) -> int | None:
    """Read the total entry count from the ZIP End of Central Directory (and
    ZIP64 EOCD when present/required) without opening ``zipfile.ZipFile``.

    Returns ``None`` if no valid EOCD (or required ZIP64 EOCD) record can be
    read from the tail of ``buf``.
    """
    import struct

    buf_len = len(buf)
    if buf_len < 22:
        return None
    min_start = max(0, buf_len - 22 - 65535)
    search_end = buf_len
    eocd_pos = -1
    while search_end >= min_start + 4:
        pos = buf.rfind(b"PK\x05\x06", min_start, search_end)
        if pos < 0:
            return None
        if pos + 22 <= buf_len:
            comment_len = struct.unpack_from("<H", buf, pos + 20)[0]
            if pos + 22 + comment_len == buf_len:
                eocd_pos = pos
                break
        search_end = pos
    if eocd_pos < 0:
        return None

    (
        _sig,
        disk_num,
        cd_disk,
        disk_entries,
        total_entries,
        cd_size,
        cd_offset,
        _comment_len,
    ) = struct.unpack_from("<4sHHHHIIH", buf, eocd_pos)

    needs_zip64 = (
        disk_num == 0xFFFF
        or cd_disk == 0xFFFF
        or disk_entries == 0xFFFF
        or total_entries == 0xFFFF
        or cd_size == 0xFFFFFFFF
        or cd_offset == 0xFFFFFFFF
    )
    has_zip64_locator = (
        eocd_pos >= 20 and buf[eocd_pos - 20 : eocd_pos - 16] == b"PK\x06\x07"
    )
    if needs_zip64 or has_zip64_locator:
        if not has_zip64_locator:
            return None
        locator_pos = eocd_pos - 20
        _loc_sig, _z64_disk, z64_offset, _total_disks = struct.unpack_from(
            "<4sIQI", buf, locator_pos
        )
        if z64_offset + 56 > locator_pos:
            return None
        if buf[z64_offset : z64_offset + 4] != b"PK\x06\x06":
            return None
        (
            _z64_sig,
            z64_size,
            _ver_made,
            _ver_needed,
            _z64_disk_num,
            _z64_cd_disk,
            z64_disk_entries,
            z64_total_entries,
            _z64_cd_size,
            _z64_cd_offset,
        ) = struct.unpack_from("<4sQHHIIQQQQ", buf, z64_offset)
        if z64_size < 44 or z64_offset + 12 + z64_size > locator_pos:
            return None
        candidates = [z64_disk_entries, z64_total_entries]
        if disk_entries != 0xFFFF:
            candidates.append(disk_entries)
        if total_entries != 0xFFFF:
            candidates.append(total_entries)
        return max(candidates)

    return max(disk_entries, total_entries)


def _is_ooxml_zip_bytes(buf: bytes) -> bool:
    """Return True if ``buf`` starts with ``PK\\x03\\x04``, has a valid EOCD
    within ``_COS_ZIP_MAX_MEMBERS``, and is a valid zip archive containing
    ``[Content_Types].xml``."""
    import io
    import zipfile

    if not buf.startswith(b"PK\x03\x04"):
        return False
    member_count = _read_zip_eocd_member_count(buf)
    if member_count is None or member_count > _COS_ZIP_MAX_MEMBERS:
        return False
    central = _count_zip_central_entries(buf, _COS_ZIP_MAX_MEMBERS)
    if central is None or central > _COS_ZIP_MAX_MEMBERS:
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
        if not content.startswith(b"PK\x03\x04"):
            return _COS_FORMAT_MISMATCH_ERROR
        member_count = _read_zip_eocd_member_count(content)
        if member_count is None:
            return _COS_FORMAT_MISMATCH_ERROR
        if member_count > _COS_ZIP_MAX_MEMBERS:
            return "压缩容器成员数量超出安全上限，不能拷贝"
        central = _count_zip_central_entries(content, _COS_ZIP_MAX_MEMBERS)
        if central is None:
            return _COS_FORMAT_MISMATCH_ERROR
        if central > _COS_ZIP_MAX_MEMBERS:
            return "压缩容器成员数量超出安全上限，不能拷贝"
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


_OLE_PPT_STREAM_UTF16LE_RE = re.compile(
    b"".join(
        b"[" + ch.lower().encode("ascii") + ch.upper().encode("ascii") + b"]\x00"
        if ch.isalpha()
        else re.escape(ch.encode("ascii")) + b"\x00"
        for ch in "PowerPoint Document"
    )
    + b"\x00\x00"
)


def _ole_has_powerpoint_stream(content: bytes) -> bool:
    """Whether an OLE2 file holds a ``PowerPoint Document`` stream (a .ppt under
    another name). Reads the directory like ``_ole_has_vba_macros``, so body
    text naming it never counts; falls back to the terminated directory name."""
    if not content.startswith(_OLE_HEADER):
        return False
    try:
        from xlrd.compdoc import CompDoc

        doc = CompDoc(content, logfile=io.StringIO())
        return any(
            getattr(node, "etype", 0) == 2
            and str(getattr(node, "name", "") or "").strip().lower() == "powerpoint document"
            for node in getattr(doc, "dirlist", ())
        )
    except Exception:
        return bool(_OLE_PPT_STREAM_UTF16LE_RE.search(content))


def _ole_has_vba_macros(content: bytes) -> bool:
    """Detect VBA macro storages/streams in a legacy OLE2 document (.xls/.doc).

    Parses the OLE2 directory tree via ``xlrd.compdoc.CompDoc`` (declared
    project dependency) so ordinary UTF-16LE body text mentioning words like
    ``VBA`` or ``Macros`` never triggers a false positive. Falls back to
    UTF-16LE null-terminated directory-name matching if the OLE container
    cannot be parsed by ``CompDoc``.
    """
    if not content.startswith(_OLE_HEADER):
        return False
    try:
        from xlrd.compdoc import CompDoc

        doc = CompDoc(content, logfile=io.StringIO())
        for node in getattr(doc, "dirlist", ()):
            if getattr(node, "etype", 0) not in (1, 2):
                continue
            name_lower = str(getattr(node, "name", "") or "").strip().lower()
            if name_lower in _OLE_VBA_DIR_NAMES or name_lower.startswith("_vba_project"):
                return True
        return False
    except Exception:
        return bool(_OLE_VBA_UTF16LE_RE.search(content))


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
    import zipfile
    from src.server.utils.secret_redactor import MIN_REDACT_SECRET_LEN, get_redactor

    _, ext = os.path.splitext(file_path)
    ext_lower = ext.lower()
    if ext_lower in _COS_LEGACY_PPT_EXTENSIONS:
        return _COS_LEGACY_PPT_REFUSED_ERROR
    if ext_lower in _COS_MACRO_OFFICE_EXTENSIONS:
        return _COS_MACRO_OFFICE_REFUSED_ERROR

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

    def _hits_xml_known_secrets(buf: bytes) -> bool:
        """Apply the same known-secret redactor check used for text copies to OOXML XML parts."""
        decode_encodings = (
            ("utf-8", "utf-16-le", "utf-16-be")
            if b"\x00" in buf
            else ("utf-8",)
        )
        redactor = get_redactor()
        for enc in decode_encodings:
            text = buf.decode(enc, errors="ignore")
            if redactor.redact(text, vault_secrets=vault_secrets) != text:
                return True
            if "&" in text:
                unescaped = html.unescape(text)
                if redactor.redact(unescaped, vault_secrets=vault_secrets) != unescaped:
                    return True
        return False

    if _hits_secret_bytes(content):
        return "文件里含有源用户的凭据，不能拷贝"

    if ext_lower in _COS_LEGACY_OFFICE_EXTENSIONS:
        # A presentation renamed to .xls / .doc keeps its "PowerPoint Document"
        # stream, where VBA hides compressed; refuse it like a .ppt.
        if _ole_has_powerpoint_stream(content):
            return _COS_LEGACY_PPT_REFUSED_ERROR
        if _ole_has_vba_macros(content):
            return _COS_MACRO_OFFICE_REFUSED_ERROR
        return None

    if ext_lower in _COS_ZIP_CONTAINER_EXTENSIONS:
        allowed_compress_types = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
        scan_state = {"total_bytes": 0, "members": 0}

        def _scan_ooxml_container(zip_bytes: bytes, depth: int) -> str | None:
            eocd_members = _read_zip_eocd_member_count(zip_bytes)
            if eocd_members is None:
                return "压缩容器无法解压核验，不能拷贝"
            if scan_state["members"] + eocd_members > _COS_ZIP_MAX_MEMBERS:
                return "压缩容器成员数量超出安全上限，不能拷贝"
            # The end record's count can understate the central directory, which
            # ZipFile parses in full; walk it up to the limit first.
            central = _count_zip_central_entries(
                zip_bytes, _COS_ZIP_MAX_MEMBERS - scan_state["members"]
            )
            if central is None:
                return "压缩容器无法解压核验，不能拷贝"
            if scan_state["members"] + central > _COS_ZIP_MAX_MEMBERS:
                return "压缩容器成员数量超出安全上限，不能拷贝"
            try:
                with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                    infos = zf.infolist()
                    if scan_state["members"] + len(infos) > _COS_ZIP_MAX_MEMBERS:
                        return "压缩容器成员数量超出安全上限，不能拷贝"
                    scan_state["members"] += len(infos)
                    for info in infos:
                        if info.compress_type not in allowed_compress_types:
                            return "压缩容器使用了不支持的压缩方式，不能拷贝"
                        if info.filename.endswith("/"):
                            if info.file_size > 0:
                                return "压缩容器格式异常，不能拷贝"
                            with zf.open(info) as dir_fp:
                                if dir_fp.read(1):
                                    return "压缩容器格式异常，不能拷贝"
                            continue
                        if info.is_dir():
                            continue
                        _, member_ext = os.path.splitext(info.filename)
                        member_ext_lower = member_ext.lower()
                        if member_ext_lower in _COS_LEGACY_PPT_EXTENSIONS:
                            return _COS_LEGACY_PPT_REFUSED_ERROR
                        if member_ext_lower in _COS_MACRO_OFFICE_EXTENSIONS:
                            return _COS_MACRO_OFFICE_REFUSED_ERROR
                        if _COS_OOXML_VBA_MACRO_PART.fullmatch(info.filename):
                            return _COS_MACRO_OFFICE_REFUSED_ERROR
                        if member_ext_lower in _COS_NESTED_ARCHIVE_EXTENSIONS:
                            return "压缩容器内嵌压缩包，不能拷贝"
                        if info.file_size > _COS_ZIP_MEMBER_MAX_BYTES:
                            return "压缩容器单个成员超出安全上限，不能拷贝"
                        chunks: list[bytes] = []
                        member_len = 0
                        with zf.open(info) as member_fp:
                            while True:
                                remaining_total = (
                                    _COS_ZIP_TOTAL_MAX_BYTES - scan_state["total_bytes"]
                                )
                                remaining_member = (
                                    _COS_ZIP_MEMBER_MAX_BYTES - member_len
                                )
                                read_limit = min(
                                    _COS_ZIP_CHUNK_BYTES,
                                    min(remaining_total, remaining_member) + 1,
                                )
                                chunk = member_fp.read(read_limit)
                                if not chunk:
                                    break
                                member_len += len(chunk)
                                if member_len > _COS_ZIP_MEMBER_MAX_BYTES:
                                    return "压缩容器单个成员超出安全上限，不能拷贝"
                                scan_state["total_bytes"] += len(chunk)
                                if scan_state["total_bytes"] > _COS_ZIP_TOTAL_MAX_BYTES:
                                    return "压缩容器解压总量超出安全上限，不能拷贝"
                                chunks.append(chunk)
                        member_bytes = b"".join(chunks)
                        if member_bytes.startswith(_OLE_HEADER):
                            if not _COS_OOXML_OLE_RAW_SCAN_PARTS.fullmatch(info.filename):
                                return _COS_EMBEDDED_OLE_ERROR
                            if _ole_has_vba_macros(member_bytes):
                                return _COS_MACRO_OFFICE_REFUSED_ERROR
                            if _hits_secret_bytes(member_bytes):
                                return "文件里含有源用户的凭据，不能拷贝"
                            continue
                        if member_ext_lower in _COS_OOXML_XML_EXTENSIONS:
                            if _hits_xml_known_secrets(member_bytes):
                                return "文件里含有密钥或源用户的凭据，不能拷贝"
                        else:
                            try:
                                decoded_member = member_bytes.decode("utf-8")
                            except UnicodeDecodeError:
                                decoded_member = None
                            if decoded_member is not None and (
                                get_redactor().redact(
                                    decoded_member, vault_secrets=vault_secrets
                                )
                                != decoded_member
                            ):
                                return "文件里含有密钥或源用户的凭据，不能拷贝"
                        if _hits_secret_with_xml_unescape(member_bytes):
                            return "文件里含有源用户的凭据，不能拷贝"
                        if (
                            member_bytes.startswith(b"PK\x03\x04")
                            or member_ext_lower in _COS_ZIP_CONTAINER_EXTENSIONS
                        ):
                            nested_eocd = _read_zip_eocd_member_count(member_bytes)
                            if nested_eocd is None:
                                return "压缩容器内嵌压缩包，不能拷贝"
                            if scan_state["members"] + nested_eocd > _COS_ZIP_MAX_MEMBERS:
                                return "压缩容器成员数量超出安全上限，不能拷贝"
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
        # DALTON-HOOK(colleague-access): dispatch whitelisted text extensions to strict UTF-8 + dual secret scan
        if _is_allowed_text_copy_extension(file_path):
            return _scan_copy_text_for_secrets(content, vault_secrets)
        return _scan_copy_bytes_for_secrets(file_path, content, vault_secrets)
    except MemoryError:
        return _COS_SCAN_FAILED_ERROR


# DALTON-HOOK(colleague-access): atomic temp-file stage + source recheck helpers for copy_file
def _bash_succeeded(res: Any) -> bool:
    if not isinstance(res, dict):
        return False
    if "exit_code" in res:
        try:
            return int(res["exit_code"]) == 0
        except (TypeError, ValueError):
            return False
    return bool(res.get("success"))


_COS_COPY_UPLOAD_DRAIN_TIMEOUT_S = 30.0
_COS_COPY_CLEANUP_TIMEOUT_S = 20.0


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if task.done() and not task.cancelled():
        task.exception()


# Late cleanups scheduled from a done callback; held here so the loop keeps them alive.
_LATE_COPY_CLEANUPS: set[asyncio.Task[Any]] = set()


def _remove_after_upload(upload_task: asyncio.Task[Any], sandbox: Any, tmp_abs: str) -> None:
    def _on_done(task: asyncio.Task[Any]) -> None:
        _consume_task_exception(task)
        cleanup = asyncio.get_running_loop().create_task(
            _remove_temp_copy_file(sandbox, tmp_abs)
        )
        _LATE_COPY_CLEANUPS.add(cleanup)
        cleanup.add_done_callback(_LATE_COPY_CLEANUPS.discard)

    upload_task.add_done_callback(_on_done)


async def _drain_cancelled_upload(
    upload_task: asyncio.Task[Any],
    tmp_abs: str,
    *,
    timeout: float = _COS_COPY_UPLOAD_DRAIN_TIMEOUT_S,
) -> asyncio.CancelledError | None:
    """Wait via ``asyncio.shield`` for an in-flight upload task to finish before
    cleaning up ``tmp_abs`` so a background upload thread cannot recreate the
    staging file after ``rm -f`` returns.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    caught_cancel: asyncio.CancelledError | None = None
    cancel_hits = 0
    while not upload_task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.warning(
                f"Timed out waiting for cancelled upload to finish; temp copy file may remain at {tmp_abs}"
            )
            upload_task.add_done_callback(_consume_task_exception)
            break
        try:
            await asyncio.wait_for(asyncio.shield(upload_task), timeout=remaining)
        except asyncio.CancelledError as exc:
            caught_cancel = exc
            cancel_hits += 1
            if cancel_hits >= 5:
                logger.warning(
                    f"Cancelled while waiting for upload to finish; temp copy file may remain at {tmp_abs}"
                )
                upload_task.add_done_callback(_consume_task_exception)
                break
        except TimeoutError:
            logger.warning(
                f"Timed out waiting for cancelled upload to finish; temp copy file may remain at {tmp_abs}"
            )
            upload_task.add_done_callback(_consume_task_exception)
            break
        except Exception:
            break
    _consume_task_exception(upload_task)
    return caught_cancel


async def _remove_temp_copy_file(sandbox: Any, tmp_abs: str) -> None:
    """Remove ``tmp_abs`` inside a shielded task so cleanup still runs even when
    the caller task is cancelled; catches ``CancelledError``, finishes cleanup
    (up to a timeout), logs a warning with ``tmp_abs`` on failure/timeout, and
    re-raises the ``CancelledError``.
    """

    async def _do_remove() -> Any:
        return await sandbox.execute_bash_command(
            command=f"rm -f -- {shlex.quote(tmp_abs)}", timeout=15
        )

    rm_task = asyncio.create_task(_do_remove())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _COS_COPY_CLEANUP_TIMEOUT_S
    caught_cancel: asyncio.CancelledError | None = None
    cancel_hits = 0
    try:
        while not rm_task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    f"Timed out removing temp copy file; temp copy file may remain at {tmp_abs}"
                )
                rm_task.add_done_callback(_consume_task_exception)
                return
            try:
                await asyncio.wait_for(asyncio.shield(rm_task), timeout=remaining)
            except asyncio.CancelledError as exc:
                caught_cancel = exc
                cancel_hits += 1
                if cancel_hits >= 5:
                    logger.warning(
                        f"Cancelled while removing temp copy file; temp copy file may remain at {tmp_abs}"
                    )
                    rm_task.add_done_callback(_consume_task_exception)
                    return
            except TimeoutError:
                logger.warning(
                    f"Timed out removing temp copy file; temp copy file may remain at {tmp_abs}"
                )
                rm_task.add_done_callback(_consume_task_exception)
                return
            except Exception as exc:
                logger.warning(f"Failed to remove temp copy file {tmp_abs}: {exc}")
                return
        if rm_task.done() and not rm_task.cancelled():
            exc = rm_task.exception()
            if exc is not None:
                logger.warning(f"Failed to remove temp copy file {tmp_abs}: {exc}")
            else:
                res = rm_task.result()
                if not _bash_succeeded(res):
                    logger.warning(
                        f"Failed to remove temp copy file {tmp_abs}: non-zero exit ({res})"
                    )
        elif rm_task.cancelled():
            logger.warning(
                f"Cleanup task cancelled; temp copy file may remain at {tmp_abs}"
            )
    finally:
        if caught_cancel is not None:
            raise caught_cancel


# DALTON-HOOK(colleague-access): shared server-side copy engine used by CoS and analyst colleague_workspace
async def _workspace_file_copy_server(
    *,
    source_workspace_id: str,
    path: str,
    dest_workspace_id: str,
    dest_owner_id: str,
    dest_path: str | None,
    tool_call_id: str,
    caller_user_id: str | None = None,
    source_viewer_ids: Sequence[str] | None = None,
    require_dest_owner_user_id: str | None = None,
    recheck_source_access: Callable[[], Awaitable[Command | None]] | None = None,
    recheck_dest_access: Callable[[], Awaitable[Command | None]] | None = None,
) -> Command:
    """Copy an allowed binary (<=50 MB) or strict UTF-8 text (<=5 MB) file server-side.

    Caller must have already verified source read authorization and destination
    write authorization before calling this helper.
    """
    try:
        from fastapi import HTTPException
        from src.server.app.workspace_files._shared import (
            _is_non_owner_blocked_path,
            _is_protected_workspace_path,
            http_file_bytes,
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
            contained_target,
            owner_work_dir,
            previous_dir_names_of,
            to_client_path as _to_client_path,
        )
        from src.server.utils.secret_redactor import (
            get_vault_secrets_for_redaction,
        )

        ws = await get_workspace(source_workspace_id)
        if not ws:
            return error_command("workspace not found", tool_call_id)
        source_owner_id = str(ws.get("user_id") or "")
        if source_viewer_ids is not None:
            source_is_non_owner = not source_owner_id or any(
                str(u) != source_owner_id for u in source_viewer_ids
            )
        elif caller_user_id is not None:
            source_is_non_owner = not source_owner_id or source_owner_id != str(
                caller_user_id
            )
        else:
            source_is_non_owner = True
        source_path_blocked = (
            _is_non_owner_blocked_path
            if source_is_non_owner
            else _is_protected_workspace_path
        )
        work_dir = owner_work_dir(ws)
        previous_dirs = previous_dir_names_of(ws)
        normalized_path = normalize_requested_path(path, work_dir, previous_dirs)
        if not normalized_path or source_path_blocked(normalized_path):
            return error_command("file not found", tool_call_id)

        owner_id = source_owner_id or dest_owner_id
        if served_from_mirror(ws.get("status")):
            file_record = await FilePersistenceService.get_file_content(
                source_workspace_id, normalized_path
            )
            if not file_record:
                return error_command("file not found", tool_call_id)
            if _is_legacy_ppt_extension(normalized_path):
                return error_command(_COS_LEGACY_PPT_REFUSED_ERROR, tool_call_id)
            if _is_macro_office_extension(normalized_path):
                return error_command(_COS_MACRO_OFFICE_REFUSED_ERROR, tool_call_id)
            if not _is_allowed_copy_extension(normalized_path):
                return error_command(_COS_COPY_UNSUPPORTED_ERROR, tool_call_id)
            max_bytes, max_label = _copy_max_bytes_for_path(normalized_path)
            try:
                size = int(file_record.get("file_size") or 0)
            except (TypeError, ValueError):
                size = 0
            if size > max_bytes:
                return error_command(
                    f"File is {size} bytes; the copy limit is "
                    f"{max_bytes} bytes ({max_label}).",
                    tool_call_id,
                )
            try:
                content = await http_file_bytes(file_record, user_id=owner_id)
            except HTTPException as e:
                return error_command(
                    f"cannot read the source file: {e.detail}", tool_call_id
                )
            if len(content) > max_bytes:
                # file_size can be missing or 0 on the record; the bytes just
                # read are the authority then.
                return error_command(
                    f"File is {len(content)} bytes; the copy limit is "
                    f"{max_bytes} bytes ({max_label}).",
                    tool_call_id,
                )
            source_client_path = normalized_path
        else:
            if _is_legacy_ppt_extension(normalized_path):
                return error_command(_COS_LEGACY_PPT_REFUSED_ERROR, tool_call_id)
            if _is_macro_office_extension(normalized_path):
                return error_command(_COS_MACRO_OFFICE_REFUSED_ERROR, tool_call_id)
            if not _is_allowed_copy_extension(normalized_path):
                return error_command(_COS_COPY_UNSUPPORTED_ERROR, tool_call_id)
            async with acquire_sandbox_to_change(source_workspace_id, owner_id) as (
                sandbox,
                live_ws,
            ):
                work_dir = owner_work_dir(live_ws)
                previous_dirs = previous_dir_names_of(live_ws)
                try:
                    contained = await _read_contained_target(
                        sandbox, path, work_dir, previous_dirs
                    )
                    # Upstream returns a ContainedFile (canonical path, bytes, stamp).
                    normalized, raw_bytes = contained.canonical, contained.content
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
            if source_path_blocked(source_client_path):
                return error_command("file not found", tool_call_id)
            if _is_legacy_ppt_extension(source_client_path):
                return error_command(_COS_LEGACY_PPT_REFUSED_ERROR, tool_call_id)
            if _is_macro_office_extension(source_client_path):
                return error_command(_COS_MACRO_OFFICE_REFUSED_ERROR, tool_call_id)
            if not _is_allowed_copy_extension(source_client_path):
                return error_command(_COS_COPY_UNSUPPORTED_ERROR, tool_call_id)
            max_bytes, max_label = _copy_max_bytes_for_path(source_client_path)
            if len(raw_bytes) > max_bytes:
                return error_command(
                    f"File is {len(raw_bytes)} bytes; the copy limit is "
                    f"{max_bytes} bytes ({max_label}).",
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

        dest_ws = await get_workspace(dest_workspace_id)
        if not dest_ws:
            return error_command(
                "destination must be Home or one of the user's own workspaces"
                if require_dest_owner_user_id is not None
                else "destination workspace not found",
                tool_call_id,
            )
        if (
            require_dest_owner_user_id is not None
            and str(dest_ws.get("user_id")) != require_dest_owner_user_id
        ):
            return error_command(
                "destination must be Home or one of the user's own workspaces",
                tool_call_id,
            )
        effective_dest_owner = str(dest_ws.get("user_id") or dest_owner_id)
        dest_is_non_owner = (
            True
            if caller_user_id is None
            else (not effective_dest_owner or effective_dest_owner != str(caller_user_id))
        )
        dest_path_blocked = (
            _is_non_owner_blocked_path
            if dest_is_non_owner
            else _is_protected_workspace_path
        )
        dest_name = (dest_path or "").strip().lstrip("/") or (
            f"shared/{source_client_path.rsplit('/', 1)[-1]}"
        )
        if served_from_mirror(dest_ws.get("status")):
            return error_command(
                f"Cannot copy into {dest_ws.get('name') or dest_workspace_id} — it is "
                f"{dest_ws.get('status')}. Wait for it to be running.",
                tool_call_id,
            )
        async with acquire_sandbox_to_change(
            dest_workspace_id, effective_dest_owner
        ) as (
            dest_sandbox,
            dest_live_ws,
        ):
            dest_work_dir = owner_work_dir(dest_live_ws)
            dest_previous = previous_dir_names_of(dest_live_ws)
            dest_rel = normalize_requested_path(
                dest_name, dest_work_dir, dest_previous
            )
            if not dest_rel or dest_path_blocked(dest_rel):
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
            dest_client_path = _to_client_path(
                dest_sandbox, dest_abs, dest_work_dir
            )
            if not dest_client_path or dest_path_blocked(dest_client_path):
                return error_command(
                    "403 Forbidden: Cannot copy into system, hidden, or virtual paths",
                    tool_call_id,
                )
            # DALTON-HOOK(colleague-access): prepare staging and destination dirs first,
            # stage under .agents/tmp/colleague-copy-<uuid> (excluded from panel, backups,
            # and transfer runtime .wsfiles-* orphan sweep), re-verify permissions right
            # before POSIX rename, and clean up (shielded) on any exception/cancellation/refusal.
            from ptc_agent.core.paths import SandboxLayout

            staging_dir = f"{dest_work_dir.rstrip('/')}/{SandboxLayout.TMP_DIR}"
            tmp_abs = f"{staging_dir}/colleague-copy-{uuid.uuid4().hex}"
            dest_parent = posixpath.dirname(dest_abs)
            prep_cmd = (
                f"mkdir -p -- {shlex.quote(staging_dir)} {shlex.quote(dest_parent)}"
            )
            prep_res = await dest_sandbox.execute_bash_command(
                command=prep_cmd, timeout=30
            )
            if not _bash_succeeded(prep_res):
                return error_command("Copy failed", tool_call_id)
            committed = False
            upload_task: asyncio.Task[Any] | None = None
            try:
                async def _do_upload() -> Any:
                    return await dest_sandbox.aupload_file_bytes(tmp_abs, content)

                upload_task = asyncio.create_task(_do_upload())
                ok = await asyncio.shield(upload_task)
                if not ok:
                    return error_command("Copy failed", tool_call_id)
                # Re-check analyst destination write permission FIRST, then source
                # read permission IMMEDIATELY before the rename so no DB query sits
                # between the source re-check and the rename.
                if recheck_dest_access is not None:
                    dest_err = await recheck_dest_access()
                    if dest_err is not None:
                        return dest_err
                if recheck_source_access is not None:
                    recheck_err = await recheck_source_access()
                    if recheck_err is not None:
                        return recheck_err
                # Avoid GNU-only `mv -T` (macOS `/bin/mv` fails with `illegal option -- T`).
                # Verify `[ ! -d "$dst" ]` first (fail if destination is a directory), then
                # run POSIX `mv -f -- "$src" "$dst"`. The tiny window between `[ ! -d ... ]`
                # and `mv -f` is accepted because only the sandbox's own agent can create a
                # directory at that location, and it already has write access there.
                dst_q = shlex.quote(dest_abs)
                src_q = shlex.quote(tmp_abs)
                mv_cmd = f"[ ! -d {dst_q} ] && mv -f -- {src_q} {dst_q}"
                mv_res = await dest_sandbox.execute_bash_command(
                    command=mv_cmd, timeout=30
                )
                if not _bash_succeeded(mv_res):
                    return error_command("Copy failed", tool_call_id)
                committed = True
            finally:
                if not committed:
                    drain_cancel: asyncio.CancelledError | None = None
                    if upload_task is not None and not upload_task.done():
                        drain_cancel = await _drain_cancelled_upload(
                            upload_task,
                            tmp_abs,
                            timeout=_COS_COPY_UPLOAD_DRAIN_TIMEOUT_S,
                        )
                    if upload_task is not None and not upload_task.done():
                        # The upload outlived the drain, so it may still write the
                        # staging file after the rm below: remove it again once
                        # the upload really ends.
                        _remove_after_upload(upload_task, dest_sandbox, tmp_abs)
                    try:
                        await _remove_temp_copy_file(dest_sandbox, tmp_abs)
                    finally:
                        if drain_cancel is not None:
                            raise drain_cancel
        return success_command(
            {
                "success": True,
                "source": {"workspace_id": source_workspace_id, "path": source_client_path},
                "workspace_id": dest_workspace_id,
                "path": dest_client_path,
                "folder_path": dest_work_dir.rstrip("/"),
                "size": len(content),
            },
            tool_call_id,
        )
    except Exception as e:
        logger.error(f"Failed to copy workspace file for CoS: {e}")
        return error_command("failed to copy workspace file", tool_call_id)


async def workspace_file_copy_cos(
    user_id: str,
    workspace_id: str | None,
    path: str | None,
    dest_path: str | None,
    tool_call_id: str,
    dest_workspace_id: str | None = None,
) -> Command:
    """Copy an allowed binary (<=50 MB) or strict UTF-8 text (<=5 MB) file into Home or another of the user's own workspaces."""
    if not workspace_id:
        return error_command("workspace_id is required for copy_file", tool_call_id)
    if not path:
        return error_command("path is required for copy_file", tool_call_id)
    if err := await verify_workspace_owner(
        workspace_id, user_id, tool_call_id, need="read"
    ):
        return err
    from src.server.database.home_workspace import get_home_workspace_id

    dest_id = (dest_workspace_id or "").strip() or get_home_workspace_id(user_id)

    async def _recheck_source_access() -> Command | None:
        return await verify_workspace_owner(
            workspace_id, user_id, tool_call_id, need="read"
        )

    return await _workspace_file_copy_server(
        source_workspace_id=workspace_id,
        path=path,
        dest_workspace_id=dest_id,
        dest_owner_id=user_id,
        dest_path=dest_path,
        tool_call_id=tool_call_id,
        caller_user_id=user_id,
        require_dest_owner_user_id=user_id,
        recheck_source_access=_recheck_source_access,
    )
# --- DALTON-HOOK(workspace-sharing-settings): end ---
