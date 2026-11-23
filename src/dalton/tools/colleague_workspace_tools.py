"""Analyst direct tool ``colleague_workspace`` for cross-workspace read & server-side copy (§2, §3).

Reuses Chief of Staff's permission checks (``verify_workspace_owner``,
``verify_thread_owner``, ``require_workspace_access`` on ``cos`` channel),
mirror/sandbox file reads (``workspace_files_list_cos``,
``workspace_file_read_cos``), shared server-side binary+UTF-8 text copy
(``_workspace_file_copy_server``), and thread readers (``threads_list``,
``extract_text_from_thread``).

Security invariants:
- Mounted only for ``role == "analyst"`` when ``CapabilityId.COLLEAGUE_SHARING`` is on.
- Dual-user verification over deduplicated ``{A, B}`` where A is caller
  (``user_id``) and B is current workspace owner (``workspace_owner_id``).
- ``Home`` (``is_home_row``) and ``Home`` stand-in (``is_reserved_row``) are
  unconditionally refused as cross-workspace targets (including ``A == B == C``).
- Writes (``copy_file``) land exclusively in the current workspace ``W_cur``
  after verifying ``W_cur`` write access on actual ``origin_channel``.
- Text returned by ``read_file`` and ``read_thread`` is wrapped in
  ``<<<EXTERNAL_UNTRUSTED_CONTENT>>>`` and marks the analyst run outbound-blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.types import Command

from ptc_agent.agent.limits import pending_read
from src.dalton.access_tiers import classify_owner_tier, get_user_tier, is_internal_tier
from src.dalton.capabilities import CapabilityId, has_capability
from src.dalton.colleague_outbound_guard import (
    OUTBOUND_GUARD_PERSIST_ERROR,
    mark_run_outbound_blocked,
)
from src.dalton.sharing import list_colleague_cos_workspaces, require_workspace_access
from src.server.database.conversation.threads_read import get_thread_auth_meta
from src.server.database.home_workspace import is_home_row, is_reserved_row
from src.server.database.workspace import get_workspace, get_workspaces_for_user
from src.server.services.thread_output import extract_text_from_thread
from src.server.utils.pg_sanitize import normalize_uuid
from src.tools.secretary._commands import (
    InjectedToolCallId,
    error_command,
    success_command,
    verify_thread_owner,
    verify_workspace_owner,
)
from src.tools.secretary.tools import (
    _workspace_file_copy_server,
    threads_list,
    workspace_file_read_cos,
    workspace_files_list_cos,
)

logger = logging.getLogger(__name__)

ColleagueWorkspaceAction = Literal[
    "list_workspaces",
    "list_files",
    "read_file",
    "copy_file",
    "list_threads",
    "read_thread",
]

_VALID_ACTIONS = frozenset(
    {
        "list_workspaces",
        "list_files",
        "read_file",
        "copy_file",
        "list_threads",
        "read_thread",
    }
)

_UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_THREAD_LINK_RE = re.compile(rf"/chat/t/({_UUID_PATTERN})(?:[/?#]|$)", re.IGNORECASE)
_WORKSPACE_LINK_RE = re.compile(rf"/chat/(?!t/)({_UUID_PATTERN})(?:[/?#]|$)", re.IGNORECASE)
_READ_ONLY_DENIAL_TOKEN = "只读"


def extract_workspace_id(raw: str | None) -> tuple[str | None, str | None]:
    """Extract workspace UUID from a raw id or ``/chat/<workspace_id>`` link.

    Returns ``(workspace_id, hint_error)``. If the caller passed a
    ``/chat/t/<thread_id>`` link in ``workspace_id``, returns a hint error so the
    model routes to ``action="read_thread"`` instead.
    """
    text = str(raw or "").strip()
    if not text:
        return None, None
    thread_match = _THREAD_LINK_RE.search(text)
    if thread_match:
        return (
            None,
            f"传入的是对话链接（/chat/t/{thread_match.group(1)}），"
            '请改用 action="read_thread" 并传入 thread_id。',
        )
    ws_match = _WORKSPACE_LINK_RE.search(text)
    if ws_match:
        return ws_match.group(1).lower(), None
    return text, None


def extract_thread_id(raw: str | None) -> tuple[str | None, str | None]:
    """Extract thread UUID from a raw id or ``/chat/t/<thread_id>`` link.

    Returns ``(thread_id, hint_error)``.
    """
    text = str(raw or "").strip()
    if not text:
        return None, None
    thread_match = _THREAD_LINK_RE.search(text)
    if thread_match:
        return thread_match.group(1).lower(), None
    ws_match = _WORKSPACE_LINK_RE.search(text)
    if ws_match:
        return (
            None,
            f"传入的是工作区链接（/chat/{ws_match.group(1)}），"
            '请传入 workspace_id，并改用 action="list_files" 或 "list_threads"。',
        )
    return text, None


def wrap_external_untrusted_content(source: str, text: str) -> str:
    """Wrap cross-workspace text with the canonical untrusted-content fence."""
    return (
        f"<<<EXTERNAL_UNTRUSTED_CONTENT>>>\n"
        f"Source: {source}\n"
        f"---\n"
        f"{text}\n"
        f"<<<END_EXTERNAL_UNTRUSTED_CONTENT>>>"
    )


def _is_disallowed_target_workspace(ws: Mapping[str, Any] | None) -> bool:
    """Return True if ``ws`` is missing, deleted, unbound, or any Home/stand-in row."""
    if not isinstance(ws, Mapping):
        return True
    if ws.get("status") in ("deleted", "unbound"):
        return True
    if is_home_row(ws) or is_reserved_row(ws) or bool(ws.get("is_home")):
        return True
    return False


def _same_workspace_id(a: str | None, b: str | None) -> bool:
    sa = str(a or "").strip()
    sb = str(b or "").strip()
    if not sa or not sb:
        return False
    na = normalize_uuid(sa) or sa.lower()
    nb = normalize_uuid(sb) or sb.lower()
    return na == nb


def _extract_command_payload(cmd: Command) -> dict[str, Any] | None:
    update = getattr(cmd, "update", None)
    if not isinstance(update, Mapping):
        return None
    messages = update.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    content = getattr(messages[0], "content", None)
    if not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_command_artifact(cmd: Command) -> Any:
    update = getattr(cmd, "update", None)
    if not isinstance(update, Mapping):
        return None
    messages = update.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    return getattr(messages[0], "artifact", None)


def _json_safe_ts(val: Any) -> str | None:
    """Convert ``datetime`` / timestamp values into JSON-serializable ISO strings."""
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return str(val.isoformat())
    return str(val)


def _is_readonly_denial_command(cmd: Command) -> bool:
    payload = _extract_command_payload(cmd)
    if not payload:
        return False
    err = str(payload.get("error") or "")
    return _READ_ONLY_DENIAL_TOKEN in err


def _stricter_access(a: str, b: str) -> str:
    """Return the stricter access tier between two visible non-hidden levels."""
    rank = {"ro": 0, "rw": 1, "owner": 2}
    ra = rank.get(a, 0)
    rb = rank.get(b, 0)
    if min(ra, rb) <= 0:
        return "ro"
    return "rw"


async def _dual_verify_workspace(
    workspace_id: str,
    identities: list[str],
    tool_call_id: str,
    *,
    need: str = "read",
) -> Command | None:
    """Verify every identity in ``identities`` passes ``verify_workspace_owner``.

    If ANY identity receives a non-read-only denial (e.g. hidden/not found),
    returns a unified ``workspace not found`` refusal. Returns a read-only
    refusal ONLY when all identities are at least readable and at least one
    was denied write access.
    """
    ro_denial: Command | None = None
    for uid in identities:
        cmd = await verify_workspace_owner(
            workspace_id, uid, tool_call_id, need=need
        )
        if cmd is None:
            continue
        if need == "write" and _is_readonly_denial_command(cmd):
            ro_denial = cmd
            continue
        return error_command("workspace not found", tool_call_id)
    if ro_denial is not None:
        return ro_denial
    return None


async def _dual_verify_thread(
    thread_id: str,
    identities: list[str],
    tool_call_id: str,
) -> Command | None:
    """Verify every identity in ``identities`` passes ``verify_thread_owner``."""
    for uid in identities:
        cmd = await verify_thread_owner(
            thread_id, uid, tool_call_id, need="read"
        )
        if cmd is not None:
            return error_command("thread not found", tool_call_id)
    return None


async def _list_visible_for_identity(uid: str) -> dict[str, dict[str, Any]]:
    """Return ``workspace_id -> entry`` for own ∪ colleague ``cos`` workspaces."""
    from src.dalton.settings import is_dalton_sharing_auth_enabled

    own_rows, _ = await get_workspaces_for_user(user_id=uid, limit=None, sort_by="activity")
    colleague_rows = (
        await list_colleague_cos_workspaces(uid)
        if is_dalton_sharing_auth_enabled()
        else []
    )
    visible: dict[str, dict[str, Any]] = {}
    for row in own_rows or []:
        if _is_disallowed_target_workspace(row):
            continue
        wid = str(row.get("workspace_id") or "").strip()
        if not wid:
            continue
        visible[wid] = {
            "workspace_id": wid,
            "name": row.get("name") or "Workspace",
            "description": row.get("description"),
            "status": row.get("status") or "stopped",
            "access": "owner",
            "owner_user_id": str(row.get("user_id") or uid),
            "owner_name": None,
            "last_activity_at": _json_safe_ts(
                row.get("last_activity_at") or row.get("updated_at")
            ),
        }
    for row in colleague_rows or []:
        if _is_disallowed_target_workspace(row):
            continue
        wid = str(row.get("workspace_id") or "").strip()
        if not wid or wid in visible:
            continue
        cos_acc = str(row.get("cos_access") or row.get("access") or "ro").strip().lower()
        if cos_acc not in ("ro", "rw"):
            continue
        visible[wid] = {
            "workspace_id": wid,
            "name": row.get("name") or "Workspace",
            "description": row.get("description"),
            "status": row.get("status") or "stopped",
            "access": cos_acc,
            "owner_user_id": str(row.get("user_id") or ""),
            "owner_name": row.get("owner_name"),
            "last_activity_at": _json_safe_ts(
                row.get("last_activity_at") or row.get("updated_at")
            ),
        }
    return visible


def create_colleague_workspace_tools() -> list[BaseTool]:
    """Build ``[colleague_workspace]`` for workspace analysts."""

    @tool("colleague_workspace")
    async def colleague_workspace(
        action: ColleagueWorkspaceAction,
        config: RunnableConfig,
        workspace_id: str | None = None,
        path: str | None = None,
        dest_path: str | None = None,
        thread_id: str | None = None,
        turns: int = 1,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> Command:
        """读取用户自己的其他工作区或同事的工作区中的文件与对话，或将文件原样复制到当前工作区。

        仅限访问 Home（个人主页）以外的其他工作区，且只向当前工作区复制文件。copy_file 仅支持 50 MB 以内的二进制文件——Office（办公文档：.xlsx、.xlsm、.docx、.pptx、.xls、.doc、.ppt）、PDF（便携式文档：.pdf）和图片（.png、.jpg、.jpeg、.gif、.webp、.bmp、.tiff）——以及 5 MB 以内的 UTF-8（通用字符编码）文本文件（.md、.markdown、.txt、.csv、.tsv、.json、.jsonl、.yaml、.yml、.py、.sql、.html、.xml、.log）；其他文本文件请用 read_file 读取。

        Args:
            action: "list_workspaces"（列出本轮可访问的其他工作区，含已停止的工作区；在同事的工作区运行时仅包含发消息的人与工作区主人均有权访问的工作区）、"list_files"（列出目标工作区文件路径）、"read_file"（读取文本文件）、"copy_file"（复制文件到当前工作区）、"list_threads"（列出最近 20 条对话）或 "read_thread"（读取对话回复，含运行中的对话）。
            workspace_id: 目标工作区的 UUID（唯一标识）或 /chat/<workspace_id> 链接；list_files、read_file、copy_file、list_threads 必填。
            path: 目标工作区内的相对路径；list_files 可选目录前缀，read_file、copy_file 必填文件路径。
            dest_path: copy_file 写入当前工作区的相对路径，默认 shared/<文件名>。
            thread_id: 目标对话的 UUID 或 /chat/t/<thread_id> 链接；read_thread 必填。
            turns: read_thread 返回最近几轮回复，默认 1（最新一轮）；传更大的整数返回最近 N 轮，传 0 返回最近最多 50 轮（对话运行中仅返回当前轮）。

        Returns:
            list_files 与 copy_file 仅返回路径（不含文件内容，copy_file 直接写入当前工作区）；read_thread 仅返回智能体回复正文与运行状态（不含工具调用明细）。
        """
        if not has_capability(CapabilityId.COLLEAGUE_SHARING):
            return error_command("colleague workspace sharing is not enabled", tool_call_id)

        act = str(action or "").strip().lower()
        if act not in _VALID_ACTIONS:
            return error_command(
                f"Unknown action: {action}. Use list_workspaces, list_files, "
                "read_file, copy_file, list_threads, or read_thread.",
                tool_call_id,
            )

        cfg = (config or {}).get("configurable") or {}
        caller_a = str(cfg.get("user_id") or "").strip()
        w_cur = str(
            cfg.get("turn_admitted_workspace_id") or cfg.get("workspace_id") or ""
        ).strip()
        origin_channel = str(cfg.get("origin_channel") or "web").strip().lower()
        if origin_channel not in ("web", "cos"):
            origin_channel = "web"

        if not caller_a or not w_cur or w_cur.lower() == "home":
            return error_command(
                "colleague_workspace requires an active workspace other than Home",
                tool_call_id,
            )

        if bool(cfg.get("tail_turn_no_new_work")):
            return error_command(
                "当前工作区的访问权限已变更，本轮仅可根据已有结果整理回复，无法访问其他工作区。",
                tool_call_id,
            )

        cur_ws = await get_workspace(w_cur)
        if not cur_ws or cur_ws.get("status") in ("deleted", "unbound") or is_reserved_row(cur_ws):
            return error_command("workspace not found", tool_call_id)

        owner_b = str(
            cfg.get("workspace_owner_id") or cur_ws.get("user_id") or ""
        ).strip()
        if not owner_b:
            return error_command("workspace not found", tool_call_id)

        is_cross_user = caller_a != owner_b
        cur_need = "write" if act == "copy_file" else "read"

        if is_cross_user:
            caller_tier = await get_user_tier(caller_a)
            owner_tier = await classify_owner_tier(owner_b)
            if not (is_internal_tier(caller_tier) and is_internal_tier(owner_tier)):
                return error_command("workspace not found", tool_call_id)
            try:
                await require_workspace_access(
                    w_cur,
                    caller_a,
                    channel=origin_channel,
                    need=cur_need,
                    workspace=cur_ws,
                )
            except HTTPException as exc:
                if exc.status_code == 403 and cur_need == "write":
                    try:
                        await require_workspace_access(
                            w_cur,
                            caller_a,
                            channel=origin_channel,
                            need="read",
                            workspace=cur_ws,
                        )
                    except HTTPException:
                        return error_command("workspace not found", tool_call_id)
                    return error_command(
                        "当前工作区为只读权限，可使用 read_file 或 read_thread 读取其他工作区内容；"
                        "如需复制文件，请在有写入权限的工作区操作。",
                        tool_call_id,
                    )
                return error_command("workspace not found", tool_call_id)

        identities = [caller_a] if not is_cross_user else [caller_a, owner_b]

        # ------------------------------------------------------------------
        # Action: list_workspaces (dual-user intersection)
        # ------------------------------------------------------------------
        if act == "list_workspaces":
            try:
                per_user = await asyncio.gather(
                    *(_list_visible_for_identity(u) for u in identities)
                )
                common_ids = set(per_user[0].keys())
                for m in per_user[1:]:
                    common_ids &= set(m.keys())
                common_ids = {
                    wid for wid in common_ids if not _same_workspace_id(wid, w_cur)
                }

                rows: list[dict[str, Any]] = []
                for wid in common_ids:
                    base = per_user[0][wid]
                    if is_cross_user:
                        eff_access = _stricter_access(
                            str(per_user[0][wid]["access"]),
                            str(per_user[1][wid]["access"]),
                        )
                    else:
                        eff_access = str(base["access"])
                    rows.append(
                        {
                            "workspace_id": wid,
                            "name": base["name"],
                            "description": base["description"],
                            "status": base["status"],
                            "access": eff_access,
                            "read_only": eff_access == "ro",
                            **(
                                {"owner_name": base["owner_name"]}
                                if base.get("owner_name")
                                else {}
                            ),
                            **(
                                {"is_own": True}
                                if not is_cross_user and eff_access == "owner"
                                else {}
                            ),
                            "last_activity_at": _json_safe_ts(
                                base.get("last_activity_at")
                            ),
                        }
                    )
                rows.sort(
                    key=lambda r: (
                        str(r.get("last_activity_at") or ""),
                        str(r.get("name") or ""),
                    ),
                    reverse=True,
                )
                return success_command(
                    {
                        "success": True,
                        "workspaces": rows,
                        "count": len(rows),
                    },
                    tool_call_id,
                )
            except Exception as exc:
                logger.error("colleague_workspace list_workspaces failed: %s", exc)
                return error_command("failed to list accessible workspaces", tool_call_id)

        # ------------------------------------------------------------------
        # Action: read_thread (thread link -> workspace Home check -> dual verify)
        # ------------------------------------------------------------------
        if act == "read_thread":
            target_tid, hint_err = extract_thread_id(thread_id)
            if hint_err:
                return error_command(hint_err, tool_call_id)
            if not target_tid:
                return error_command(
                    "thread_id is required for read_thread", tool_call_id
                )
            meta = await get_thread_auth_meta(target_tid)
            target_ws_id = str((meta or {}).get("workspace_id") or "").strip()
            if not target_ws_id:
                return error_command("thread not found", tool_call_id)
            target_ws = await get_workspace(target_ws_id)
            if _is_disallowed_target_workspace(target_ws):
                return error_command("thread not found", tool_call_id)
            if err := await _dual_verify_thread(target_tid, identities, tool_call_id):
                return err
            if _same_workspace_id(target_ws_id, w_cur):
                return error_command(
                    "目标对话属于当前工作区，请直接查看当前对话上下文或本工作区的历史对话记录。",
                    tool_call_id,
                )

            from src.dalton.settings import is_dalton_sharing_auth_enabled

            if is_dalton_sharing_auth_enabled() and not await mark_run_outbound_blocked(
                config, role="analyst", workspace_id=w_cur
            ):
                return error_command(OUTBOUND_GUARD_PERSIST_ERROR, tool_call_id)

            try:
                tz = str(cfg.get("timezone") or "UTC")
                output = await extract_text_from_thread(target_tid, turns, timezone=tz)
            except Exception as exc:
                logger.error("colleague_workspace read_thread failed for %s: %s", target_tid, exc)
                return error_command("failed to retrieve thread output", tool_call_id)

            if output is None:
                result = {
                    "success": True,
                    "text": "",
                    "status": "not_found",
                    "thread_id": target_tid,
                    "workspace_id": target_ws_id,
                }
            else:
                out_dict = output._asdict()
                raw_text = str(out_dict.get("text") or "")
                wrapped_text = (
                    wrap_external_untrusted_content(
                        f"colleague_thread:{target_tid}", raw_text
                    )
                    if raw_text
                    else ""
                )
                result = {
                    "success": True,
                    "thread_id": target_tid,
                    **out_dict,
                    "workspace_id": out_dict.get("workspace_id") or target_ws_id,
                    "text": wrapped_text,
                }
            running = result.get("status") == "running"
            return success_command(
                result, tool_call_id, pending_read() if running else None
            )

        # ------------------------------------------------------------------
        # Workspace-targeted actions: list_files, read_file, copy_file, list_threads
        # ------------------------------------------------------------------
        target_ws_id, hint_err = extract_workspace_id(workspace_id)
        if hint_err:
            return error_command(hint_err, tool_call_id)
        if not target_ws_id:
            return error_command(
                f"workspace_id is required for {act}", tool_call_id
            )

        if act in ("read_file", "copy_file") and _same_workspace_id(target_ws_id, w_cur):
            return error_command(
                "目标工作区就是当前工作区，请直接用本地工具（Read、Write、Bash 等）操作本工作区文件。",
                tool_call_id,
            )

        target_ws = await get_workspace(target_ws_id)
        if _is_disallowed_target_workspace(target_ws):
            return error_command("workspace not found", tool_call_id)

        if err := await _dual_verify_workspace(
            target_ws_id, identities, tool_call_id, need="read"
        ):
            return err

        if act == "list_files":
            cmd = await workspace_files_list_cos(
                caller_a, target_ws_id, path, tool_call_id
            )
            payload = _extract_command_payload(cmd)
            if payload and payload.get("success") is True:
                raw_files = payload.get("files") or []
                if isinstance(raw_files, list):
                    payload["files"] = [
                        wrap_external_untrusted_content(
                            f"colleague_workspace:{target_ws_id}/file",
                            str(f),
                        )
                        for f in raw_files
                    ]
                    payload["count"] = len(raw_files)
                else:
                    payload["files"] = []
                    payload["count"] = 0
                return success_command(payload, tool_call_id)
            return cmd

        if act == "list_threads":
            cmd = await threads_list(
                caller_a, target_ws_id, tool_call_id, cfg.get("thread_id")
            )
            payload = _extract_command_payload(cmd)
            if payload and payload.get("success") is True:
                raw_threads = payload.get("threads")
                if isinstance(raw_threads, list):
                    wrapped_threads: list[Any] = []
                    for item in raw_threads:
                        if isinstance(item, dict):
                            copied = dict(item)
                            raw_title = copied.get("title")
                            if isinstance(raw_title, str) and raw_title:
                                tid_label = str(
                                    copied.get("conversation_thread_id")
                                    or target_ws_id
                                )
                                copied["title"] = wrap_external_untrusted_content(
                                    f"colleague_thread:{tid_label}/title",
                                    raw_title,
                                )
                            wrapped_threads.append(copied)
                        else:
                            wrapped_threads.append(item)
                    payload["threads"] = wrapped_threads
                return success_command(
                    payload, tool_call_id, _extract_command_artifact(cmd)
                )
            return cmd

        if act == "read_file":
            if not path:
                return error_command("path is required for read_file", tool_call_id)
            from src.dalton.settings import is_dalton_sharing_auth_enabled

            if is_dalton_sharing_auth_enabled() and not await mark_run_outbound_blocked(
                config, role="analyst", workspace_id=w_cur
            ):
                return error_command(OUTBOUND_GUARD_PERSIST_ERROR, tool_call_id)
            cmd = await workspace_file_read_cos(
                caller_a, target_ws_id, path, tool_call_id
            )
            payload = _extract_command_payload(cmd)
            if payload and payload.get("success") is True:
                file_path = str(payload.get("path") or path)
                raw_content = str(payload.get("content") or "")
                payload["content"] = wrap_external_untrusted_content(
                    f"colleague_workspace:{target_ws_id}/{file_path}",
                    raw_content,
                )
                return success_command(payload, tool_call_id)
            return cmd

        # act == "copy_file"
        if not path:
            return error_command("path is required for copy_file", tool_call_id)
        from src.dalton.settings import is_dalton_sharing_auth_enabled

        if is_dalton_sharing_auth_enabled() and not await mark_run_outbound_blocked(
            config, role="analyst", workspace_id=w_cur
        ):
            return error_command(OUTBOUND_GUARD_PERSIST_ERROR, tool_call_id)
        async def _recheck_source_access() -> Command | None:
            refreshed = await get_workspace(target_ws_id)
            if _is_disallowed_target_workspace(refreshed):
                return error_command("workspace not found", tool_call_id)
            return await _dual_verify_workspace(
                target_ws_id, identities, tool_call_id, need="read"
            )

        return await _workspace_file_copy_server(
            source_workspace_id=target_ws_id,
            path=path,
            dest_workspace_id=w_cur,
            dest_owner_id=owner_b,
            dest_path=dest_path,
            tool_call_id=tool_call_id,
            require_dest_owner_user_id=None,
            recheck_source_access=_recheck_source_access,
        )

    return [colleague_workspace]


__all__ = [
    "ColleagueWorkspaceAction",
    "create_colleague_workspace_tools",
    "extract_thread_id",
    "extract_workspace_id",
    "wrap_external_untrusted_content",
]
