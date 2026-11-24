"""Direct LangChain tool `dalton_monitor` for publishing and managing Dalton Monitors."""

from __future__ import annotations

import json
import logging
import shlex
from collections.abc import Collection
from typing import Any, Literal

from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import ValidationError

from ptc_agent.core.paths import ALWAYS_HIDDEN_DIR_NAMES
from src.dalton.access_tiers import get_user_tier
from src.dalton.auth import resolve_caller_home_workspace_id
from src.dalton.monitors import service as monitor_service
from src.dalton.monitors.models import (
    MonitorCategory,
    MonitorVisibility,
    PublishMonitorRequest,
    RepublishMonitorRequest,
    UpdateMonitorRequest,
)
from src.dalton.monitors.permissions import is_company_tier
from src.dalton.settings import get_dalton_settings, is_artifact_publishing_enabled
from src.dalton.tools.knowledge_tools import _extract_run_identity
from src.server.database import artifacts as artifacts_db
from src.server.database.artifacts import Artifact
from src.server.database.workspace import get_workspace as db_get_workspace
from src.server.services.artifacts import snapshot as artifact_snapshot
from src.server.services.artifacts.manifest import MAX_BYTES, MAX_FILES
from src.server.services.persistence.file import FilePersistenceService
from src.server.services.served_paths import PAGE_LIBRARY_DIR_NAMES, may_be_artifact
from src.server.services.workspace_file_containment import contained_relative_path
from src.server.services.workspace_files import (
    acquire_sandbox_to_change,
    owned_workspace_on_computer,
    owner_work_dir,
    previous_dir_names_of,
)

logger = logging.getLogger(__name__)

MonitorActionLiteral = Literal["publish", "update", "republish", "list"]

PUBLIC_LINK_FORBIDDEN_MESSAGE = "公开链接只能由管理员在面板设置里打开"
PUBLIC_REPUBLISH_FORBIDDEN_MESSAGE = "公开面板的更新只能由管理员在面板设置里操作"
PUBLISHING_DISABLED_MESSAGE = "发布功能未开启（DALTON_ARTIFACT_PUBLISHING_ENABLED 未启用）。"
DALTON_DISABLED_MESSAGE = "Dalton 模式未启用，监控面板不可用。"
EXTERNAL_SCOPE_FORBIDDEN_MESSAGE = "权限不足：外部成员只能发布或设置“仅自己（own）”可见的监控面板。"
SCOPE_CONFIRMATION_REQUIRED_MESSAGE = (
    "将监控面板设为「指定成员（members）」或「全公司（company）」可见（或变更指定成员名单）"
    "需要用户在本次对话中明确确认。请先向用户确认具体的可见范围与成员名单，获得明确同意后再将 "
    "user_confirmed_scope=true 传入重试。"
)

_VALID_ACTIONS = frozenset({"publish", "update", "republish", "list"})


def _err(message: str, **extra: Any) -> str:
    payload: dict[str, Any] = {"ok": False, "error": message}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _ok(payload: dict[str, Any]) -> str:
    out = {"ok": True, **payload}
    return json.dumps(out, ensure_ascii=False)


_UNLOADABLE_DIR_NAMES: tuple[str, ...] = tuple(
    sorted((ALWAYS_HIDDEN_DIR_NAMES - PAGE_LIBRARY_DIR_NAMES) | {"__pycache__"})
)
_OVER_CAP_HINT = (
    "改发入口 HTML 文件（只带页面实际引用的文件），或把页面需要的文件单独放进一个文件夹"
)

# Skip symlinks (`S_ISLNK`) during folder enumeration to match the service layer's
# file listings (`FilePersistenceService.get_file_tree` with `kind = 'file'` and
# `list_sandbox_files`): otherwise a symlink inside the folder (such as
# `alias.json -> excluded.json`) would be seeded into `loaded` under the alias
# name and bypass `left_out_files`, hidden-path filters, or folder boundaries.
# Symlinks explicitly referenced by the HTML page are still resolved canonically
# by `walk_manifest` / `_SandboxReader`.
_SANDBOX_LIST_FOLDER_SCRIPT = (
    "import json, os, stat, sys\n"
    "work_dir, folder_rel = sys.argv[1], sys.argv[2]\n"
    "work_real = os.path.realpath(work_dir)\n"
    "target = os.path.realpath(os.path.join(work_real, folder_rel))\n"
    "if target != work_real and not target.startswith(work_real + os.sep):\n"
    "    print(json.dumps({'is_dir': False, 'files': []}))\n"
    "    sys.exit(0)\n"
    "if not os.path.isdir(target):\n"
    "    print(json.dumps({'is_dir': False, 'files': []}))\n"
    "    sys.exit(0)\n"
    "unloadable = set(sys.argv[3].split(',')) if sys.argv[3] else set()\n"
    "files = []\n"
    "stack = [(target, folder_rel.strip('/'))]\n"
    "while stack:\n"
    "    cur_dir, rel_prefix = stack.pop()\n"
    "    try:\n"
    "        it = os.scandir(cur_dir)\n"
    "    except OSError:\n"
    "        continue\n"
    "    with it:\n"
    "        for entry in it:\n"
    "            name = entry.name\n"
    "            if name.startswith('.'):\n"
    "                continue\n"
    "            rel = f'{rel_prefix}/{name}' if rel_prefix else name\n"
    "            try:\n"
    "                st = entry.stat(follow_symlinks=False)\n"
    "            except OSError:\n"
    "                continue\n"
    "            if stat.S_ISDIR(st.st_mode):\n"
    "                if name not in unloadable:\n"
    "                    stack.append((entry.path, rel))\n"
    "            elif stat.S_ISREG(st.st_mode):\n"
    "                files.append([rel, st.st_size])\n"
    "print(json.dumps({'is_dir': True, 'files': files}))\n"
)


def _is_hidden_workspace_path(rel_path: str) -> bool:
    return any(part.startswith(".") for part in rel_path.split("/") if part)


def _translate_http_error(exc: HTTPException, *, path: str | None = None, slug: str | None = None) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("code") or "")
        if code == "artifact_publishing_disabled":
            return PUBLISHING_DISABLED_MESSAGE
        if code == "file_missing":
            if path:
                return f"文件不存在（如果是文件夹，请确认其中包含 index.html）：{path}"
            if slug:
                return f"面板关联的工作区文件不存在：{slug}"
            return "文件不存在。"
        if code == "files_changed":
            return "文件还在变化，等生成完成后再发布。"
        if code == "needs_computer":
            return "当前工作区尚未启动或未绑定电脑环境，请先启动工作区后再发布。"
        if code == "too_many_files":
            limit = detail.get("limit")
            return f"页面依赖文件数量超过上限（最多 {limit} 个）。" if limit else "页面依赖文件数量超过上限。"
        if code == "too_many_bytes":
            limit = detail.get("limit")
            return f"页面及依赖文件总大小超过上限（{limit} 字节）。" if limit else "页面及依赖文件总大小超过上限。"
        return f"操作失败：{json.dumps(detail, ensure_ascii=False)}"

    msg = str(detail or "").strip()
    if exc.status_code == 403:
        if msg and not msg.startswith("权限不足"):
            return f"权限不足：{msg}"
        return msg or "权限不足，无法执行此操作。"
    if msg == "Workspace not found":
        return "找不到当前工作区或无权访问该工作区。"
    if msg == "Home is not on a computer yet":
        return "Home 工作区尚未绑定到电脑环境，暂无法发布监控面板。"
    if msg == "监控面板不存在" and slug:
        return f"监控面板「{slug}」不存在。"
    return msg or f"操作失败（HTTP {exc.status_code}）。"


def _translate_unexpected_error(exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        return f"监控面板操作失败：{detail}"
    return "监控面板操作失败，发生内部异常。"


def _format_member_candidate(member: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": str(member.get("user_id") or ""),
        "name": member.get("name"),
        "email": member.get("email"),
        "tier": member.get("tier"),
    }


async def _resolve_member_identifiers(
    user_id: str,
    member_names_or_emails: list[str],
) -> tuple[list[str] | None, str | None, list[dict[str, Any]] | None]:
    """Resolve member names/emails against ``monitor_service.list_company_members_for_user``.

    Never guesses on missing or duplicate names: returns candidates for the model to ask the user.
    """
    queries = [str(item or "").strip() for item in member_names_or_emails if str(item or "").strip()]
    if not queries:
        return None, "选择「指定成员（members）」可见时，请至少提供一位成员的姓名或邮箱。", None

    members = await monitor_service.list_company_members_for_user(user_id)
    resolved_ids: list[str] = []
    seen_ids: set[str] = set()
    ambiguous_entries: list[tuple[str, list[dict[str, Any]]]] = []
    unresolved_entries: list[tuple[str, list[dict[str, Any]]]] = []

    for q in queries:
        ql = q.lower()
        matches = [
            m
            for m in members
            if str(m.get("email") or "").strip().lower() == ql
        ]
        if not matches:
            matches = [
                m
                for m in members
                if str(m.get("user_id") or "").strip() == q
            ]
        if not matches:
            matches = [
                m
                for m in members
                if str(m.get("name") or "").strip().lower() == ql
            ]

        if len(matches) == 1:
            uid = str(matches[0].get("user_id") or "").strip()
            if uid and uid not in seen_ids:
                seen_ids.add(uid)
                resolved_ids.append(uid)
        elif len(matches) > 1:
            ambiguous_entries.append((q, [_format_member_candidate(m) for m in matches]))
        else:
            partial = [
                _format_member_candidate(m)
                for m in members
                if ql in str(m.get("name") or "").strip().lower()
                or ql in str(m.get("email") or "").strip().lower()
            ]
            unresolved_entries.append((q, partial))

    if ambiguous_entries or unresolved_entries:
        parts: list[str] = []
        all_candidates: list[dict[str, Any]] = []
        seen_cands: set[str] = set()
        for q, cands in ambiguous_entries:
            desc = "、".join(
                f"{c.get('name') or '(无姓名)'} <{c.get('email') or c['user_id']}>"
                for c in cands
            )
            parts.append(f"成员「{q}」存在重名（{len(cands)} 位：{desc}），请向用户确认具体是哪一位并使用邮箱或 user_id 重试")
            for c in cands:
                cid = c["user_id"]
                if cid not in seen_cands:
                    seen_cands.add(cid)
                    all_candidates.append(c)
        for q, cands in unresolved_entries:
            if cands:
                desc = "、".join(
                    f"{c.get('name') or '(无姓名)'} <{c.get('email') or c['user_id']}>"
                    for c in cands
                )
                parts.append(f"未找到完全匹配的成员「{q}」，候选成员：{desc}，请向用户确认")
                for c in cands:
                    cid = c["user_id"]
                    if cid not in seen_cands:
                        seen_cands.add(cid)
                        all_candidates.append(c)
            else:
                parts.append(f"未在公司成员列表中找到「{q}」，请向用户确认正确的成员姓名或邮箱")
        return None, "；".join(parts) + "。", all_candidates

    return resolved_ids, None, None


async def _list_sandbox_folder_files(
    ws_id: str,
    user_id: str,
    folder_rel: str,
) -> tuple[bool, list[tuple[str, int]], str | None]:
    """Walk ``folder_rel`` on a running sandbox using page-load rules instead of backup exclusions."""
    async with acquire_sandbox_to_change(ws_id, user_id) as (sandbox, held_ws):
        work_dir = owner_work_dir(held_ws)
        runtime = getattr(sandbox, "runtime", None)
        if runtime is None:
            return False, [], "无法读取运行中的沙箱文件，请稍后重试。"
        cmd = (
            f"python3 -I -c {shlex.quote(_SANDBOX_LIST_FOLDER_SCRIPT)} "
            f"{shlex.quote(work_dir)} {shlex.quote(folder_rel)} "
            f"{shlex.quote(','.join(_UNLOADABLE_DIR_NAMES))}"
        )
        result = await runtime.exec(cmd, timeout=30)
    if getattr(result, "exit_code", None) != 0:
        return False, [], "读取工作区文件夹失败，请稍后重试。"
    try:
        parsed = json.loads(str(getattr(result, "stdout", "") or "{}"))
    except ValueError:
        return False, [], "读取工作区文件夹失败，请稍后重试。"
    is_dir = bool(parsed.get("is_dir"))
    raw_files = parsed.get("files") or []
    candidates = [
        (str(item[0]), int(item[1] or 0))
        for item in raw_files
        if isinstance(item, (list, tuple)) and len(item) >= 2
    ]
    return is_dir, candidates, None


async def _resolve_publish_target(
    workspace: dict[str, Any],
    user_id: str,
    raw_path: str,
) -> tuple[str | None, bool, list[tuple[str, int]] | None, str | None]:
    """Resolve ``raw_path`` into ``(entry_rel, is_folder, prelisted_candidates, error)``."""
    work_dir = owner_work_dir(workspace)
    prev_dirs = previous_dir_names_of(workspace)
    stripped = raw_path.strip().rstrip("/")
    rel = contained_relative_path(stripped, work_dir, prev_dirs)
    if not rel or rel == ".":
        return None, False, None, "无效的工作区文件路径"

    last_segment = rel.rsplit("/", 1)[-1]
    if last_segment.lower().endswith((".html", ".htm")):
        return rel, False, None, None
    if "." not in last_segment:
        return f"{rel}/index.html", True, None, None

    ws_id = str(workspace["workspace_id"])
    if workspace.get("status") == "running":
        is_dir, candidates, list_err = await _list_sandbox_folder_files(ws_id, user_id, rel)
        if list_err is not None:
            return None, False, None, list_err
        if is_dir:
            return f"{rel}/index.html", True, candidates, None
    else:
        tree = await FilePersistenceService.get_file_tree(ws_id)
        candidates = [
            (str(item["path"]), int(item.get("size") or 0))
            for item in tree
            if item.get("path")
        ]
        prefix = f"{rel}/"
        if any(p.lstrip("/").startswith(prefix) for p, _ in candidates):
            return f"{rel}/index.html", True, candidates, None

    return (
        None,
        False,
        None,
        f"只能发布 HTML 文件（.html / .htm）或包含 index.html 的文件夹，当前路径不是 HTML 文件：{raw_path}",
    )


async def _collect_folder_files(
    workspace: dict[str, Any],
    user_id: str,
    folder_rel: str,
    *,
    left_out_paths: Collection[str] = (),
    prelisted: list[tuple[str, int]] | None = None,
) -> tuple[list[str], set[str], str | None]:
    """List page-loadable non-hidden files under ``folder_rel`` and enforce per-monitor caps."""
    ws_id = str(workspace["workspace_id"])
    clean_folder = folder_rel.rstrip("/")
    if prelisted is not None:
        candidates = prelisted
    elif workspace.get("status") == "running":
        is_dir, candidates, list_err = await _list_sandbox_folder_files(
            ws_id, user_id, clean_folder
        )
        if list_err is not None:
            return [], set(), list_err
        if not is_dir:
            return [], set(), f"文件不存在（如果是文件夹，请确认其中包含 index.html）：{clean_folder}"
    else:
        tree = await FilePersistenceService.get_file_tree(ws_id)
        candidates = [
            (str(item["path"]), int(item.get("size") or 0))
            for item in tree
            if item.get("path")
        ]

    prefix = f"{clean_folder}/"
    entry_rel = f"{clean_folder}/index.html"
    excluded_set = set(left_out_paths) - {entry_rel}
    excluded_paths: set[str] = set()
    matched: dict[str, int] = {}
    for rel, size in candidates:
        norm = rel.lstrip("/")
        if not norm.startswith(prefix):
            continue
        if _is_hidden_workspace_path(norm) or not may_be_artifact(norm):
            continue
        if norm in excluded_set:
            excluded_paths.add(norm)
            continue
        matched[norm] = max(0, size)

    if entry_rel not in matched:
        return [], excluded_paths, f"文件不存在（如果是文件夹，请确认其中包含 index.html）：{clean_folder}"

    if len(matched) > MAX_FILES:
        return (
            [],
            excluded_paths,
            f"文件夹内文件数量超过上限（当前 {len(matched)} 个，最多 {MAX_FILES} 个）。请{_OVER_CAP_HINT}。",
        )
    total_bytes = sum(matched.values())
    if total_bytes > MAX_BYTES:
        return (
            [],
            excluded_paths,
            f"文件夹内文件总大小超过上限（当前 {total_bytes} 字节，最多 {MAX_BYTES} 字节）。请{_OVER_CAP_HINT}。",
        )

    ordered = [entry_rel, *sorted(p for p in matched if p != entry_rel)]
    return ordered, excluded_paths, None


async def _prepare_publish_snapshot(
    workspace: dict[str, Any],
    artifact: Artifact,
    clean_path: str,
    caller_id: str,
    *,
    is_folder: bool,
    prelisted: list[tuple[str, int]] | None = None,
) -> tuple[list[str] | None, list[str], list[str], str | None, int, str | None]:
    """Collect folder files (when ``is_folder``) and compute ``page_sha256``."""
    excluded_paths: set[str] = set()
    if is_folder:
        folder_rel = artifact.path.removesuffix("/index.html")
        folder_files, excluded_paths, folder_err = await _collect_folder_files(
            workspace,
            caller_id,
            folder_rel,
            left_out_paths=artifact.left_out_files,
            prelisted=prelisted,
        )
        if folder_err is not None:
            return None, [], [], None, 0, folder_err
        entry_rel = folder_files[0]
        # Dynamic fetch() paths inside a folder only survive _Walked when also seeded as loaded.
        loaded_files = [p for p in folder_files if p != entry_rel]
        # Pass excluded folder files as `earlier` (matching PublishDialog's remembered loads):
        # `_Walked` treats `earlier` as optional, so paths in `artifact.left_out_files` stay in
        # `snap.left_out` and survive the service layer's `left_out_files=[e.path for e in snap.left_out]`.
        earlier_files = sorted(excluded_paths)
    else:
        loaded_files = []
        earlier_files = []

    try:
        status_res = await artifact_snapshot.status(
            artifact,
            user_id=caller_id,
            loaded=tuple(loaded_files),
            earlier=tuple(earlier_files),
        )
    except (artifact_snapshot.SnapshotRefused, artifact_snapshot.SnapshotUnavailable) as exc:
        return None, [], [], None, 0, _translate_http_error(
            monitor_service._translate_snapshot_error(exc), path=clean_path
        )

    if status_res.over_cap is not None:
        over_exc = artifact_snapshot.SnapshotRefused(
            status_res.over_cap.code, limit=status_res.over_cap.limit
        )
        return None, [], [], None, 0, _translate_http_error(
            monitor_service._translate_snapshot_error(over_exc), path=clean_path
        )

    snap = status_res.snapshot
    if snap is None:
        return None, [], [], None, 0, "无法读取页面文件，请稍后重试。"

    reviewed_files = [e.path for e in snap.entries]
    included_set = set(reviewed_files)
    left_out_count = len(
        {p for p in excluded_paths if p not in included_set}
        | {e.path for e in snap.left_out if e.path not in included_set}
    )
    return reviewed_files, loaded_files, earlier_files, snap.page_sha256, left_out_count, None


async def _prepare_republish_snapshot(
    current_mon: dict[str, Any],
    caller_id: str,
    slug: str,
) -> tuple[list[str] | None, str | None, str | None]:
    """Compute ``files`` and ``page_sha256`` for ``republish`` so mid-write edits fail as ``files_changed``."""
    if current_mon.get("source_kind") not in (None, "workspace"):
        return None, None, None
    if current_mon.get("can_manage") is False:
        return None, None, None
    artifact_id = str(current_mon.get("artifact_id") or "").strip()
    if not artifact_id:
        return None, None, None

    workspace_owner_id = str(current_mon.get("owner_user_id") or "").strip() or caller_id
    ws_id = str(current_mon.get("workspace_id") or "").strip()
    if ws_id:
        ws = await db_get_workspace(ws_id)
        if ws and ws.get("user_id"):
            workspace_owner_id = str(ws["user_id"])

    artifact = await artifacts_db.get_artifact(artifact_id, user_id=workspace_owner_id)
    if artifact is None:
        return None, None, None

    try:
        status_res = await artifact_snapshot.status(
            artifact,
            user_id=workspace_owner_id,
            loaded=(),
        )
    except (artifact_snapshot.SnapshotRefused, artifact_snapshot.SnapshotUnavailable) as exc:
        return None, None, _translate_http_error(
            monitor_service._translate_snapshot_error(exc), slug=slug
        )

    if status_res.over_cap is not None:
        over_exc = artifact_snapshot.SnapshotRefused(
            status_res.over_cap.code, limit=status_res.over_cap.limit
        )
        return None, None, _translate_http_error(
            monitor_service._translate_snapshot_error(over_exc), slug=slug
        )

    snap = status_res.snapshot
    if snap is None:
        return None, None, None

    return [e.path for e in snap.entries], snap.page_sha256, None


def _format_monitor_result(
    action: str,
    detail: dict[str, Any],
    *,
    note: str | None = None,
    left_out: int | None = None,
) -> str:
    mon = detail.get("monitor") or {}
    slug_val = str(mon.get("slug") or "")
    title_val = str(mon.get("title") or "")
    vis_val = str(mon.get("visibility") or "")
    seq_val = int(mon.get("published_seq") or 0)
    versions = detail.get("versions") or []
    latest_version_at = (
        versions[0].get("published_at")
        if versions and isinstance(versions[0], dict)
        else mon.get("updated_at")
    )
    payload: dict[str, Any] = {
        "action": action,
        "slug": slug_val,
        "title": title_val,
        "visibility": vis_val,
        "category": mon.get("category"),
        "version": seq_val,
        "published_seq": seq_val,
        "keep_updated": bool(mon.get("keep_updated")),
        "latest_version_at": latest_version_at,
        "url": f"/monitors/{slug_val}",
    }
    if "cut" in detail:
        payload["cut"] = bool(detail["cut"])
    if left_out is not None and left_out > 0:
        payload["left_out"] = left_out
    if note:
        payload["note"] = note
    return _ok(payload)


def build_dalton_monitor_tool(*, allow_home_workspace: bool = False) -> BaseTool:
    """Build ``dalton_monitor``; Chief of Staff resolves empty/``home`` workspace to Home."""

    @tool("dalton_monitor")
    async def _dalton_monitor(
        action: MonitorActionLiteral,
        path: str | None = None,
        slug: str | None = None,
        title: str | None = None,
        category: MonitorCategory | None = None,
        visibility: MonitorVisibility | None = None,
        member_names_or_emails: list[str] | None = None,
        keep_updated: bool | None = None,
        user_confirmed_scope: bool = False,
        *,
        config: RunnableConfig,
    ) -> str:
        """将工作区 HTML 网页发布为监控面板（/monitors/<slug>，slug 为短标识）。

        `publish`=发布新面板或用文件夹重新打包；`republish`=用工作区当前文件出新版本（只带上次文件与静态引用；新增动态文件用 publish）；`update`=只改设置；`list`=列出可管理面板。
        仅支持 .html/.htm 或含 index.html 的文件夹；文件夹发布会带上文件夹内所有可被页面加载的非隐藏文件。页面运行时按变量拼路径读取数据文件时，把页面和数据放进一个文件夹再发布该文件夹。可见范围默认 own（仅自己；外部成员限 own），禁 public（公开）；设 members（指定成员）/company（全公司）或改名单须用户确认传 user_confirmed_scope=true。

        Args:
            path: publish 工作区相对路径；slug: update/republish 必填。
            member_names_or_emails: members 填姓名/邮箱/user_id（用户 ID）。
            keep_updated: 对话后自动更新。

        Returns:
            JSON：含 url（地址）/version（版本）；candidates（候选成员）须问用户，files_changed（文件变更中）等写完重试。
        """
        act = str(action or "").strip().lower()
        if act not in _VALID_ACTIONS:
            return _err(f"不支持的 action `{action}`，可选值：publish、update、republish、list。")

        if not get_dalton_settings().enabled:
            return _err(DALTON_DISABLED_MESSAGE)

        if not is_artifact_publishing_enabled():
            return _err(PUBLISHING_DISABLED_MESSAGE)

        user_id, workspace_id, _thread_id, _ = _extract_run_identity(config)
        if not user_id:
            return _err("缺少当前用户信息（user_id），无法操作监控面板。")

        from src.dalton.settings import is_dalton_sharing_auth_enabled

        sharing_auth_enabled = is_dalton_sharing_auth_enabled()
        if act in ("publish", "update", "republish") and sharing_auth_enabled:
            from src.dalton.colleague_outbound_guard import (
                OUTBOUND_GUARD_REFUSAL_MESSAGE,
                is_run_outbound_blocked,
            )

            tool_role = "chief_of_staff" if allow_home_workspace else "analyst"
            if await is_run_outbound_blocked(config, role=tool_role):
                return _err(OUTBOUND_GUARD_REFUSAL_MESSAGE, code="colleague_outbound_blocked")

        clean_path_ctx: str | None = None
        clean_slug_ctx: str | None = None
        try:
            if act == "list":
                visible = await monitor_service.list_monitors_for_user(user_id)
                manageable = [
                    {
                        "slug": str(item.get("slug") or ""),
                        "title": str(item.get("title") or ""),
                        "visibility": str(item.get("visibility") or ""),
                        "category": item.get("category"),
                        "source_kind": item.get("source_kind"),
                        "version": item.get("published_seq"),
                        "latest_version_at": item.get("updated_at"),
                        "keep_updated": bool(item.get("keep_updated")),
                        "url": f"/monitors/{item.get('slug')}",
                    }
                    for item in visible
                    if bool(item.get("can_manage"))
                    and item.get("source_kind") != "data_job"
                ]
                return _ok({"action": "list", "monitors": manageable, "count": len(manageable)})

            if act == "republish":
                clean_slug = str(slug or "").strip().lower()
                if not clean_slug:
                    return _err("重新发布监控面板（republish）需要提供面板 slug。")
                clean_slug_ctx = clean_slug

                current_detail = await monitor_service.get_monitor_detail(
                    clean_slug, viewer_id=user_id
                )
                current_mon = current_detail.get("monitor") or {}
                current_visibility = str(current_mon.get("visibility") or "")

                if current_visibility == "public":
                    return _err(PUBLIC_REPUBLISH_FORBIDDEN_MESSAGE)

                caller_tier = await get_user_tier(user_id)
                if not is_company_tier(caller_tier) and current_visibility != "own":
                    return _err(EXTERNAL_SCOPE_FORBIDDEN_MESSAGE)

                # Require explicit owner confirmation if workspace has pending_outbound_confirm
                mon_ws_id = str(current_mon.get("workspace_id") or "")
                if sharing_auth_enabled and mon_ws_id:
                    from src.dalton.colleague_outbound_guard import (
                        PENDING_CONFIRM_REQUIRED_MESSAGE,
                        is_workspace_pending_outbound_confirm,
                    )

                    mon_ws = await db_get_workspace(mon_ws_id)
                    if (
                        is_workspace_pending_outbound_confirm(mon_ws)
                        and user_confirmed_scope is not True
                    ):
                        return _err(
                            PENDING_CONFIRM_REQUIRED_MESSAGE,
                            code="pending_outbound_confirm",
                        )

                files_list, page_sha256, snap_err = await _prepare_republish_snapshot(
                    current_mon, user_id, clean_slug
                )
                if snap_err is not None:
                    return _err(snap_err)

                detail = await monitor_service.republish_workspace_monitor(
                    clean_slug,
                    RepublishMonitorRequest(files=files_list, page_sha256=page_sha256),
                    caller_id=user_id,
                    reject_if_pending_outbound_confirm=(
                        sharing_auth_enabled and user_confirmed_scope is not True
                    ),
                )
                return _format_monitor_result("republish", detail)

            # Shared visibility guard for `publish` and `update`.
            if visibility == "public":
                return _err(PUBLIC_LINK_FORBIDDEN_MESSAGE)

            if act == "publish":
                if allow_home_workspace and (
                    not workspace_id or workspace_id.strip().lower() == "home"
                ):
                    workspace_id = await resolve_caller_home_workspace_id(user_id)
                if not workspace_id or workspace_id.strip().lower() == "home":
                    return _err("缺少当前工作区信息（workspace_id），无法发布工作区页面。")

                cleaned_path = str(path or "").strip()
                if not cleaned_path:
                    return _err("请提供工作区内的 HTML 文件路径（.html/.htm）或包含 index.html 的文件夹路径（path）。")
                stripped_path = cleaned_path.rstrip("/")
                if not stripped_path:
                    return _err("请提供有效的工作区文件路径或文件夹路径（path）。")
                clean_path_ctx = stripped_path

                try:
                    workspace = await owned_workspace_on_computer(workspace_id, user_id)
                except HTTPException as exc:
                    if exc.status_code == 403:
                        return _err(
                            _translate_http_error(
                                HTTPException(status_code=404, detail="Workspace not found"),
                                path=clean_path_ctx,
                            )
                        )
                    return _err(_translate_http_error(exc, path=clean_path_ctx))

                if workspace.get("status") == "deleted":
                    return _err("找不到当前工作区或无权访问该工作区。")

                entry_rel, is_folder, prelisted_candidates, target_err = (
                    await _resolve_publish_target(workspace, user_id, cleaned_path)
                )
                if target_err is not None or entry_rel is None:
                    return _err(target_err or "无效的文件路径。")

                effective_visibility: MonitorVisibility = visibility or "own"
                caller_tier = await get_user_tier(user_id)
                if not is_company_tier(caller_tier) and (
                    effective_visibility != "own" or bool(member_names_or_emails)
                ):
                    return _err(EXTERNAL_SCOPE_FORBIDDEN_MESSAGE)

                artifact = await artifacts_db.ensure_artifact(
                    str(workspace["workspace_id"]), entry_rel
                )
                existing_mon = await monitor_service.get_monitor_by_artifact(
                    artifact.artifact_id, viewer_id=user_id
                )
                if existing_mon is not None:
                    existing_visibility = str(existing_mon.get("visibility") or "")
                    if existing_visibility == "public":
                        return _err(PUBLIC_REPUBLISH_FORBIDDEN_MESSAGE)
                    if not is_company_tier(caller_tier) and existing_visibility != "own":
                        return _err(EXTERNAL_SCOPE_FORBIDDEN_MESSAGE)

                if sharing_auth_enabled:
                    from src.dalton.colleague_outbound_guard import (
                        PENDING_CONFIRM_REQUIRED_MESSAGE,
                        is_workspace_pending_outbound_confirm,
                    )

                    if (
                        is_workspace_pending_outbound_confirm(workspace)
                        and user_confirmed_scope is not True
                    ):
                        return _err(
                            PENDING_CONFIRM_REQUIRED_MESSAGE,
                            code="pending_outbound_confirm",
                        )

                if effective_visibility in ("members", "company") and user_confirmed_scope is not True:
                    return _err(SCOPE_CONFIRMATION_REQUIRED_MESSAGE)

                if effective_visibility != "members" and member_names_or_emails:
                    return _err(
                        f"可见范围为「{effective_visibility}」时无需指定 member_names_or_emails；仅 visibility='members' 时使用。"
                    )

                resolved_members: list[str] = []
                if effective_visibility == "members":
                    if not member_names_or_emails:
                        return _err("选择「指定成员（members）」可见时，请通过 member_names_or_emails 提供至少一位成员的姓名或邮箱。")
                    ids, member_err, candidates = await _resolve_member_identifiers(
                        user_id, member_names_or_emails
                    )
                    if member_err is not None or ids is None:
                        extra = {"candidates": candidates} if candidates is not None else {}
                        return _err(member_err or "成员解析失败。", **extra)
                    resolved_members = ids

                files_list, loaded_files, earlier_files, page_sha256, left_out_count, snap_err = (
                    await _prepare_publish_snapshot(
                        workspace,
                        artifact,
                        stripped_path,
                        user_id,
                        is_folder=is_folder,
                        prelisted=prelisted_candidates,
                    )
                )
                if snap_err is not None:
                    return _err(snap_err)

                try:
                    req = PublishMonitorRequest(
                        workspace_id=workspace_id,
                        path=entry_rel if is_folder else stripped_path,
                        title=title,
                        slug=slug,
                        category=category or "other",
                        visibility=effective_visibility,
                        member_user_ids=resolved_members,
                        keep_updated=bool(keep_updated) if keep_updated is not None else False,
                        files=files_list,
                        loaded=loaded_files,
                        earlier=earlier_files,
                        page_sha256=page_sha256,
                    )
                except ValidationError as exc:
                    return _err(f"参数格式有误（{exc.errors()[0].get('msg', 'invalid input')}）。")

                detail = await monitor_service.publish_workspace_monitor(
                    req,
                    caller_id=user_id,
                    reject_if_pending_outbound_confirm=(
                        sharing_auth_enabled and user_confirmed_scope is not True
                    ),
                )
                return _format_monitor_result(
                    "publish",
                    detail,
                    left_out=left_out_count if left_out_count > 0 else None,
                )

            # act == "update"
            clean_slug = str(slug or "").strip().lower()
            if not clean_slug:
                return _err("更新监控面板设置（update）需要提供面板 slug。")
            clean_slug_ctx = clean_slug

            caller_tier = await get_user_tier(user_id)
            if not is_company_tier(caller_tier) and (
                (visibility is not None and visibility != "own") or bool(member_names_or_emails)
            ):
                return _err(EXTERNAL_SCOPE_FORBIDDEN_MESSAGE)

            if visibility in ("own", "company") and member_names_or_emails:
                return _err(
                    f"可见范围为「{visibility}」时无需指定 member_names_or_emails；仅 visibility='members' 时使用。"
                )

            if member_names_or_emails is not None and not member_names_or_emails:
                return _err("指定成员名单不能为空；若要改为仅自己可见，请将 visibility 设为 'own'。")

            scope_changing = visibility in ("members", "company") or (
                visibility is None and member_names_or_emails is not None
            )
            if scope_changing and user_confirmed_scope is not True:
                return _err(SCOPE_CONFIRMATION_REQUIRED_MESSAGE)

            current_detail = await monitor_service.get_monitor_detail(
                clean_slug, viewer_id=user_id
            )
            current_mon = current_detail.get("monitor") or {}
            current_visibility = str(current_mon.get("visibility") or "")
            current_members = [
                str(m) for m in (current_mon.get("member_user_ids") or []) if str(m).strip()
            ]

            if not is_company_tier(caller_tier) and current_visibility != "own":
                return _err(EXTERNAL_SCOPE_FORBIDDEN_MESSAGE)

            resolved_member_ids: list[str] | None = None
            preserved_members_note: str | None = None
            if member_names_or_emails is not None:
                ids, member_err, candidates = await _resolve_member_identifiers(
                    user_id, member_names_or_emails
                )
                if member_err is not None or ids is None:
                    extra = {"candidates": candidates} if candidates is not None else {}
                    return _err(member_err or "成员解析失败。", **extra)
                resolved_member_ids = ids
            elif visibility == "members":
                if current_visibility == "members":
                    resolved_member_ids = None
                    preserved_members_note = f"沿用原名单（{len(current_members)} 人）"
                else:
                    return _err(
                        "从其他可见范围改为「指定成员（members）」时，请通过 member_names_or_emails 提供至少一位成员的姓名、邮箱或 user_id。"
                    )
            elif visibility in ("own", "company"):
                resolved_member_ids = []

            if (
                title is None
                and category is None
                and visibility is None
                and resolved_member_ids is None
                and keep_updated is None
            ):
                return _err(
                    "请至少提供一项要更新的设置（title、category、visibility、member_names_or_emails 或 keep_updated）。"
                )

            try:
                update_req = UpdateMonitorRequest(
                    title=title,
                    category=category,
                    visibility=visibility,
                    member_user_ids=resolved_member_ids,
                    keep_updated=keep_updated,
                )
            except ValidationError as exc:
                return _err(f"参数格式有误（{exc.errors()[0].get('msg', 'invalid input')}）。")

            detail = await monitor_service.update_monitor_settings(
                clean_slug, update_req, caller_id=user_id
            )
            return _format_monitor_result("update", detail, note=preserved_members_note)
        except HTTPException as exc:
            if isinstance(exc.detail, dict) and exc.detail.get("code") == "pending_outbound_confirm":
                from src.dalton.colleague_outbound_guard import PENDING_CONFIRM_REQUIRED_MESSAGE

                return _err(
                    str(exc.detail.get("message") or PENDING_CONFIRM_REQUIRED_MESSAGE),
                    code="pending_outbound_confirm",
                )
            return _err(_translate_http_error(exc, path=clean_path_ctx, slug=clean_slug_ctx))
        except Exception as exc:
            logger.exception("dalton_monitor action=%s failed with unexpected error", act)
            return _err(_translate_unexpected_error(exc))

    return _dalton_monitor


def create_monitor_tools(*, allow_home_workspace: bool = False) -> list[BaseTool]:
    """Return ``[dalton_monitor]`` bound to the caller's role."""
    return [build_dalton_monitor_tool(allow_home_workspace=allow_home_workspace)]


__all__ = [
    "DALTON_DISABLED_MESSAGE",
    "EXTERNAL_SCOPE_FORBIDDEN_MESSAGE",
    "MonitorActionLiteral",
    "PUBLIC_LINK_FORBIDDEN_MESSAGE",
    "PUBLIC_REPUBLISH_FORBIDDEN_MESSAGE",
    "PUBLISHING_DISABLED_MESSAGE",
    "SCOPE_CONFIRMATION_REQUIRED_MESSAGE",
    "build_dalton_monitor_tool",
    "create_monitor_tools",
]
