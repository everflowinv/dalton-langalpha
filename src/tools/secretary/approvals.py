"""Hand-offs and new workspaces the user approved in advance.

The settings live in ``other_preference``, which the agent cannot write. New
workspaces are made with ``la``, whose approval reads its setting itself
(``cli_approval``). The hand-off's is read when the model proposes the call
and recorded against that call in the thread state, never when the tool runs:
a tool that paused re-runs from the top on resume, so a setting changed while
its card was pending would otherwise skip an answer the user already gave,
and a hand-off they declined would run.
"""

import logging
import re
from typing import Annotated, Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import PrivateStateAttr
from typing_extensions import NotRequired

from ptc_agent.agent.pending_calls import pending_calls
from src.tools.secretary._commands import HANDOFF_TOOL_NAME

logger = logging.getLogger(__name__)

AUTO_APPROVE_HANDOFFS = "auto_approve_handoffs"
#: Lets ``manage_workspaces(action="create")`` (or ``la workspaces create``)
#: run without asking in the app; the web's Settings and the create dialog's
#: "always" save it.
AUTO_APPROVE_WORKSPACE_CREATION = "auto_approve_workspace_creation"
# DALTON-HOOK(computer-cli-tokens-off): server-side manage_workspaces(create) uses standing approval.
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
    # --- DALTON-HOOK(cos-settings-tools): begin ---
    # Align proposal-time duplicate detection with Phase 1/Phase 2 folder-key
    # and canonical ticker rules (e.g. ``Q3/Q4`` vs ``Q3-Q4``, ``Research  Notes``
    # vs ``Research Notes``, ``00700`` vs ``700.HK``, ``BRK-B`` vs ``BRK.B``)
    # without folding non-ticker Unicode variants like ``计划①`` vs ``计划1``.
    from src.server.database.workspace_names import (
        WorkspaceNameInvalid,
        workspace_name_key,
    )

    try:
        if workspace_name_key(new) == workspace_name_key(existing):
            return True
    except WorkspaceNameInvalid:
        pass
    from src.dalton.coverage.identity import canonical_workspace_ticker_key

    new_key = canonical_workspace_ticker_key(new)
    existing_key = canonical_workspace_ticker_key(existing)
    if new_key is not None and existing_key is not None and new_key == existing_key:
        return True
    # --- DALTON-HOOK(cos-settings-tools): end ---
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
    except Exception as exc:
        # DALTON-HOOK(cos-settings-tools): log only whitelisted fields without raw exception text
        logger.error(
            "duplicate_workspace_check_failed exc_type=%s",
            type(exc).__name__,
        )
        return None, False
    for holder in holders:
        if is_duplicate_name(name, holder.get("name") or ""):
            return holder, True
    return None, True


# --- DALTON-HOOK(cos-settings-tools): begin ---
BOOTSTRAP_CLOUD_COMPUTER_LABEL = "新建托管云端电脑"


def _sandbox_config_for_bootstrap() -> Any:
    """Return the active sandbox configuration for hosted cloud bootstrap checks."""
    from src.server.services.workspace_manager import WorkspaceManager

    return WorkspaceManager.get_instance().config.sandbox


async def build_create_workspace_snapshot(
    user_id: str, call: dict[str, Any]
) -> dict[str, Any] | None:
    """Resolve and freeze target computer & duplicate state for ``manage_workspaces(action="create")``.

    Runs once in ``StandingApprovalMiddleware.aafter_model`` before the tool
    executes or pauses on ``interrupt()``. Both standing auto-approval and the
    confirmation card / resumed execution read this single snapshot.
    """
    from ptc_agent.agent.tools.computer_reach import ComputerUnreachable
    from ptc_agent.core.sandbox.providers import MACHINE_KINDS, RECREATABLE_KINDS
    from src.server.database.computer import (
        get_computer,
        get_computer_roster,
        get_computers_for_user,
        get_primary_computer,
    )
    from src.server.database.user import get_user
    from src.server.models.computer import ComputerStatus
    from src.server.services.computer_errors import NeedsComputer
    from src.server.services.computer_reach import pick_computer
    from src.tools.secretary.utils import spoken_computer_name

    args = call.get("args")
    if not isinstance(args, dict):
        return None
    raw_name = args.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return None

    name = raw_name
    raw_desc = args.get("description")
    description = raw_desc if isinstance(raw_desc, str) else ""
    raw_computer = args.get("computer")
    computer_wanted = (
        raw_computer.strip()
        if isinstance(raw_computer, str) and raw_computer.strip()
        else None
    )
    call_id = str(call.get("id") or "")

    snapshot: dict[str, Any] = {
        "action": "create_workspace",
        "tool_call_id": call_id,
        "workspace_name": name,
        "workspace_description": description,
        "requested_computer": computer_wanted,
        "computer_id": None,
        "computer_name": None,
        "computer_kind": None,
        "computer_is_own_machine": False,
        "bootstrap_cloud_primary": False,
        "duplicate_workspace_id": None,
        "duplicate_workspace_name": None,
        "duplicate_check_unavailable": False,
        "precheck_error": None,
        "precheck_error_code": None,
    }

    target: dict[str, Any] | None = None
    try:
        if computer_wanted is not None:
            roster = await get_computer_roster(user_id)
            try:
                picked = pick_computer(roster, computer_wanted)
            except ComputerUnreachable as exc:
                snapshot["precheck_error"] = str(exc)
                snapshot["precheck_error_code"] = "computer_not_found"
                return snapshot
            target = await get_computer(str(picked["computer_id"]))
            if target is None or str(target.get("user_id")) != str(user_id):
                snapshot["precheck_error"] = "Computer not found"
                snapshot["precheck_error_code"] = "computer_not_found"
                return snapshot
        else:
            target = await get_primary_computer(user_id)
            if target is None:
                user_row = await get_user(user_id)
                existing = await get_computers_for_user(user_id)
                own_only = (user_row or {}).get("computer_choice") == "own_machine"
                default_kind: str | None = None
                cloud_ok = False
                try:
                    sandbox_cfg = _sandbox_config_for_bootstrap()
                    default_kind = getattr(sandbox_cfg, "provider", None)
                    creatable = tuple(sandbox_cfg.creatable_providers())
                    cloud_ok = (
                        isinstance(default_kind, str)
                        and default_kind in RECREATABLE_KINDS
                        and default_kind in creatable
                    )
                except Exception as exc:
                    logger.warning(
                        "create_snapshot_sandbox_config_failed exc_type=%s",
                        type(exc).__name__,
                    )
                    cloud_ok = False

                if cloud_ok and not own_only:
                    snapshot["computer_id"] = None
                    snapshot["bootstrap_cloud_primary"] = True
                    snapshot["computer_name"] = BOOTSTRAP_CLOUD_COMPUTER_LABEL
                    snapshot["computer_kind"] = default_kind
                    snapshot["computer_is_own_machine"] = False
                else:
                    if existing:
                        names = ", ".join(
                            f'"{spoken_computer_name(c.get("name"))}"' for c in existing
                        )
                        if not cloud_ok:
                            snapshot["precheck_error"] = (
                                f"当前部署默认提供商（{default_kind or 'unknown'}）不支持自动创建托管云端主电脑。"
                                f"请通过 computer 参数指定已有电脑后重新发起：{names}。"
                            )
                            snapshot["precheck_error_code"] = "provider_not_offered"
                        else:
                            snapshot["precheck_error"] = (
                                "当前账号已设置为使用自有电脑且尚未设定主电脑。"
                                f"请通过 computer 参数指定已有电脑后重新发起：{names}。"
                            )
                            snapshot["precheck_error_code"] = "needs_computer"
                    else:
                        if not cloud_ok:
                            snapshot["precheck_error"] = (
                                f"当前部署默认提供商（{default_kind or 'unknown'}）不支持自动创建托管云端主电脑，"
                                "且你名下暂无可用电脑。请先在电脑页面连接一台电脑，"
                                "或联系管理员配置托管云端提供商（daytona / docker / e2b）。"
                            )
                            snapshot["precheck_error_code"] = "provider_not_offered"
                        else:
                            snapshot["precheck_error"] = str(NeedsComputer.no_computer())
                            snapshot["precheck_error_code"] = "needs_computer"
                    return snapshot
    except Exception as exc:
        logger.warning(
            "create_snapshot_computer_lookup_failed exc_type=%s",
            type(exc).__name__,
        )
        snapshot["precheck_error"] = "Failed to inspect target computer; please try again."
        snapshot["precheck_error_code"] = "computer_lookup_failed"
        return snapshot

    if target is not None:
        cfg = target.get("config") or {}
        if cfg.get("retire_when_empty") and not target.get("is_primary"):
            snapshot["precheck_error"] = "Computer not found"
            snapshot["precheck_error_code"] = "computer_not_found"
            return snapshot
        if cfg.get("archiving"):
            snapshot["precheck_error"] = (
                "This computer is currently being archived and cannot accept new workspaces."
            )
            snapshot["precheck_error_code"] = "computer_archiving"
            return snapshot
        if target.get("status") == ComputerStatus.ERROR:
            snapshot["precheck_error"] = (
                f'Computer "{spoken_computer_name(target.get("name"))}" is in an error state '
                "and cannot accept new workspaces until repaired or rebuilt in Computers."
            )
            snapshot["precheck_error_code"] = "computer_error"
            return snapshot
        if target.get("status") in (
            ComputerStatus.CREATING,
            ComputerStatus.STARTING,
        ) and not target.get("provider_ref"):
            snapshot["precheck_error"] = (
                "The user's computer is still being prepared, so no workspace can "
                "be created on it yet. Tell the user to try again once it is ready."
            )
            snapshot["precheck_error_code"] = "computer_preparing"
            return snapshot

        kind = str(target.get("kind") or "")
        snapshot["computer_id"] = str(target["computer_id"])
        snapshot["computer_name"] = spoken_computer_name(target.get("name"))
        snapshot["computer_kind"] = kind
        snapshot["computer_is_own_machine"] = kind in MACHINE_KINDS
        snapshot["bootstrap_cloud_primary"] = False

    holder, confirmed = await duplicate_workspace(user_id, name)
    if holder is not None:
        snapshot["duplicate_workspace_id"] = str(holder.get("workspace_id"))
        snapshot["duplicate_workspace_name"] = holder.get("name")
    elif not confirmed:
        snapshot["duplicate_check_unavailable"] = True
    return snapshot


def _snapshot_allows_standing_create(snapshot: dict[str, Any] | None) -> bool:
    """Whether ``snapshot`` may use standing auto-approval for workspace creation."""
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("precheck_error"):
        return False
    if snapshot.get("computer_is_own_machine"):
        return False
    if snapshot.get("duplicate_workspace_id") is not None:
        return False
    if snapshot.get("duplicate_check_unavailable"):
        return False
    return True


def _is_archive_workspace_call(call: dict[str, Any]) -> bool:
    """Whether ``call`` is a ``manage_workspaces(action="archive")`` proposal."""
    if call.get("name") != "manage_workspaces":
        return False
    args = call.get("args")
    return isinstance(args, dict) and args.get("action") == "archive"


async def build_archive_workspace_snapshot(
    user_id: str,
    call: dict[str, Any],
    *,
    current_computer_id: str | None = None,
) -> dict[str, Any] | None:
    """Read-only precheck and whole-computer snapshot for ``manage_workspaces(action="archive")``.

    Runs once in ``StandingApprovalMiddleware.aafter_model`` before the tool
    node executes. Never calls ``resolve_binding`` so it has zero side effects.
    """
    from ptc_agent.core.sandbox.providers import provider_supports_archive
    from src.server.database.computer import (
        computer_has_blocking_archive_move,
        get_computer_for_workspace,
        get_live_workspaces_for_archive_snapshot,
    )
    from src.server.database.home_workspace import is_reserved_row
    from src.server.database.pool import get_db_connection
    from src.server.database.workspace import get_workspace
    from src.server.database.workspace_folders import (
        computer_command_running,
        computer_run_in_progress,
    )
    from src.server.models.computer import ComputerStatus
    from src.server.services.home_placement import home_turn_computer
    from src.tools.secretary.utils import spoken_computer_name

    args = call.get("args")
    if not isinstance(args, dict):
        return None
    raw_ws_id = args.get("workspace_id")
    if not isinstance(raw_ws_id, str) or not raw_ws_id.strip():
        return None

    workspace_id = raw_ws_id.strip()
    call_id = str(call.get("id") or "")
    snapshot: dict[str, Any] = {
        "action": "archive_workspace",
        "tool_call_id": call_id,
        "workspace_id": workspace_id,
        "workspace_name": "",
        "computer_id": None,
        "computer_name": None,
        "computer_kind": None,
        "expected_workspaces": [],
        "precheck_error": None,
        "precheck_error_code": None,
    }

    try:
        ws = await get_workspace(workspace_id)
        if not ws or str(ws.get("user_id")) != str(user_id):
            snapshot["precheck_error"] = "workspace not found"
            snapshot["precheck_error_code"] = "workspace_not_found"
            return snapshot
        if is_reserved_row(ws):
            snapshot["precheck_error"] = "Home cannot be archived."
            snapshot["precheck_error_code"] = "home_protected"
            return snapshot

        snapshot["workspace_id"] = str(ws["workspace_id"])
        snapshot["workspace_name"] = str(ws.get("name") or "")

        computer = await get_computer_for_workspace(workspace_id)
        if computer is None:
            snapshot["precheck_error"] = "workspace not found"
            snapshot["precheck_error_code"] = "workspace_not_found"
            return snapshot

        computer_id = str(computer["computer_id"])
        computer_name = spoken_computer_name(computer.get("name"))
        computer_kind = str(computer.get("kind") or "")
        computer_status = str(computer.get("status") or "")
        snapshot["computer_id"] = computer_id
        snapshot["computer_name"] = computer_name
        snapshot["computer_kind"] = computer_kind

        active_cos_computer_id = current_computer_id
        if active_cos_computer_id is None:
            try:
                cos_comp, _ = await home_turn_computer(user_id)
                if cos_comp is not None and cos_comp.get("computer_id"):
                    active_cos_computer_id = str(cos_comp["computer_id"])
            except Exception as exc:
                logger.warning(
                    "archive_snapshot_home_turn_lookup_failed exc_type=%s",
                    type(exc).__name__,
                )

        if (
            computer_status == ComputerStatus.RUNNING
            and active_cos_computer_id is not None
            and computer_id == active_cos_computer_id
        ):
            snapshot["precheck_error"] = (
                f'电脑「{computer_name}」正是当前研究总监对话运行所在的电脑，无法在对话中直接归档。'
                "请前往网页端沙箱设置面板（概览页）先停止该电脑，再执行整机归档。"
            )
            snapshot["precheck_error_code"] = "running_cos_computer"
            return snapshot

        if not provider_supports_archive(computer_kind):
            snapshot["precheck_error"] = (
                f'电脑「{computer_name}」（提供商：{computer_kind or "unknown"}）不支持冷归档；'
                "只有 Daytona 云端电脑支持冷归档，当前电脑停止即可释放计算资源。"
            )
            snapshot["precheck_error_code"] = "archive_unsupported"
            return snapshot

        cfg = computer.get("config") or {}
        if cfg.get("archiving"):
            snapshot["precheck_error"] = (
                f'电脑「{computer_name}」正在归档中，无需重复发起。'
            )
            snapshot["precheck_error_code"] = "computer_archiving"
            return snapshot
        if cfg.get("retire_when_empty"):
            snapshot["precheck_error"] = (
                f'电脑「{computer_name}」正在清理退役，暂不可归档。'
            )
            snapshot["precheck_error_code"] = "computer_busy"
            return snapshot
        spec_change = computer.get("spec_change")
        if (
            isinstance(spec_change, dict)
            and spec_change.get("state") == "in_progress"
            and not computer.get("spec_change_stale")
        ):
            snapshot["precheck_error"] = (
                f'电脑「{computer_name}」正在变更配置，暂不可归档。'
            )
            snapshot["precheck_error_code"] = "computer_busy"
            return snapshot

        if computer_status != ComputerStatus.STOPPED:
            snapshot["precheck_error"] = (
                f'电脑「{computer_name}」当前状态为 {computer_status}，只有已停止的电脑才能归档。'
                "请先在网页端沙箱设置面板（概览页）停止该电脑后再发起归档。"
            )
            snapshot["precheck_error_code"] = "not_stopped"
            return snapshot

        if not computer.get("provider_ref"):
            snapshot["precheck_error"] = (
                f'电脑「{computer_name}」尚未创建云端沙箱，无法归档。'
            )
            snapshot["precheck_error_code"] = "no_sandbox"
            return snapshot

        has_blocking_move = await computer_has_blocking_archive_move(computer_id)
        if has_blocking_move:
            snapshot["precheck_error"] = (
                f'电脑「{computer_name}」上有工作区正在迁移，请等待迁移完成后再发起归档。'
            )
            snapshot["precheck_error_code"] = "move_in_progress"
            return snapshot

        has_active_work = False
        async with get_db_connection() as conn, conn.cursor() as cur:
            if await computer_run_in_progress(cur, computer_id) or await computer_command_running(
                cur, computer_id
            ):
                has_active_work = True
        if has_active_work:
            snapshot["precheck_error"] = (
                f'电脑「{computer_name}」仍有活跃运行或后台命令，暂不可归档。'
            )
            snapshot["precheck_error_code"] = "computer_busy"
            return snapshot

        snapshot["expected_workspaces"] = await get_live_workspaces_for_archive_snapshot(
            computer_id
        )
        return snapshot
    except Exception as exc:
        logger.warning(
            "archive_snapshot_precheck_failed workspace_id=%s exc_type=%s",
            workspace_id,
            type(exc).__name__,
        )
        snapshot["precheck_error"] = "Failed to inspect target computer; please try again."
        snapshot["precheck_error_code"] = "archive_precheck_failed"
        return snapshot
# --- DALTON-HOOK(cos-settings-tools): end ---


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


async def standing_approval(user_id: str, key: str) -> bool:
    """Whether the user turned on the ``other_preference`` setting ``key``."""
    from src.server.database.user import get_user_preferences

    try:
        # Uncached: a read racing the write that turned it off could cache
        # the old yes.
        prefs = await get_user_preferences(user_id, cached=False)
    except Exception:
        # Asking is the default, so a setting that cannot be read asks
        # instead of failing the call.
        logger.warning(
            f"Could not read approval settings for user {user_id}; asking",
            exc_info=True,
        )
        return False
    other = (prefs or {}).get("other_preference") or {}
    return other.get(key) is True


def _settings_for(call: dict[str, Any]) -> str | None:
    """The setting that, granted, lets ``call`` run without its card."""
    args = call.get("args")
    if not isinstance(args, dict):
        args = {}
    name = call.get("name")
    if name == HANDOFF_TOOL_NAME:
        return AUTO_APPROVE_HANDOFFS
    # DALTON-HOOK(computer-cli-tokens-off): CoS workspace create tool uses standing approval.
    if name == "manage_workspaces" and args.get("action") == "create":
        return AUTO_APPROVE_WORKSPACE_CREATION
    return None


async def _granted(user_id: str) -> set[str]:
    from src.server.database.user import get_user_preferences

    try:
        # Uncached: a read racing the write that turned it off could cache
        # the old yes.
        prefs = await get_user_preferences(user_id, cached=False)
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
    # DALTON-HOOK(cos-settings-tools): persist pre-card confirmation snapshots by tool_call_id
    approval_snapshots: NotRequired[
        Annotated[dict[str, dict[str, Any]], PrivateStateAttr]
    ]


class StandingApprovalMiddleware(AgentMiddleware):
    """Records which of a model call's tool calls need no approval card."""

    state_schema = StandingApprovalState

    def __init__(self, user_id: str | None) -> None:
        super().__init__()
        self._user_id = user_id

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        if not self._user_id:
            return None
        _, calls = pending_calls(state.get("messages") or [])
        wanted = {
            call["id"]: needed
            for call in calls
            if (needed := _settings_for(call))
        }
        # --- DALTON-HOOK(cos-settings-tools): begin ---
        archive_calls = {
            call["id"]: call
            for call in calls
            if _is_archive_workspace_call(call) and call.get("id")
        }
        if not wanted and not archive_calls:
            return None
        granted = await _granted(self._user_id) if wanted else set()
        # DALTON-HOOK(workspace-duplicate-guard): the duplicate check reads the proposed call
        by_id = {call["id"]: call for call in calls}
        existing_snapshots = state.get("approval_snapshots")
        if not isinstance(existing_snapshots, dict):
            existing_snapshots = {}
        approval_snapshots: dict[str, dict[str, Any]] = {}
        preapproved_calls = []
        for call_id, needed in wanted.items():
            if needed is AUTO_APPROVE_WORKSPACE_CREATION:
                prev_snap = existing_snapshots.get(call_id)
                snapshot = (
                    dict(prev_snap)
                    if isinstance(prev_snap, dict)
                    else await build_create_workspace_snapshot(
                        self._user_id, by_id[call_id]
                    )
                )
                if snapshot is not None:
                    approval_snapshots[call_id] = snapshot
                if needed in granted and _snapshot_allows_standing_create(snapshot):
                    preapproved_calls.append(call_id)
                continue
            if needed not in granted:
                continue
            preapproved_calls.append(call_id)
        for call_id, call in archive_calls.items():
            prev_snap = existing_snapshots.get(call_id)
            snapshot = (
                dict(prev_snap)
                if isinstance(prev_snap, dict)
                else await build_archive_workspace_snapshot(self._user_id, call)
            )
            if snapshot is not None:
                approval_snapshots[call_id] = snapshot
        update: dict[str, Any] = {"preapproved_calls": preapproved_calls}
        if approval_snapshots:
            update["approval_snapshots"] = approval_snapshots
        return update
        # --- DALTON-HOOK(cos-settings-tools): end ---


def preapproved(state: dict[str, Any], tool_call_id: str) -> bool:
    """Whether this call was approved in advance when the model proposed it."""
    return tool_call_id in (state.get("preapproved_calls") or ())


# --- DALTON-HOOK(cos-settings-tools): begin ---
def approval_snapshot(
    state: dict[str, Any] | None, tool_call_id: str
) -> dict[str, Any] | None:
    """Return the frozen confirmation snapshot for ``tool_call_id``, if any."""
    if not isinstance(state, dict):
        return None
    snapshots = state.get("approval_snapshots")
    if not isinstance(snapshots, dict):
        return None
    snap = snapshots.get(tool_call_id)
    return dict(snap) if isinstance(snap, dict) else None
# --- DALTON-HOOK(cos-settings-tools): end ---
