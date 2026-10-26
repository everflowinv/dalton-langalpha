"""Computer lifecycle routes shared with workspace aliases.

Aliases resolve workspace to computer and call these actions so transitions
cannot drift while both API paths remain live.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

from ptc_agent.core.sandbox.runtime import SandboxGoneError, SandboxTransientError
from src.server.services.persistence.sync_result import BackupIncomplete
from src.server.app.background_starts import schedule_start
from src.server.app.status_stream import (
    SSE_HEADERS,
    sse_status_event,
    status_event_stream,
)
from src.server.database.computer import (
    computer_capacity_lock,
    get_computer,
    get_computers_for_user,
    observed_layout_version,
    rename_computer as db_rename_computer,
    update_computer_status,
)
from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.workspace import (
    count_live_workspaces_by_computer,
    get_live_workspace_folders_for_computer,
)
from src.server.database.workspace_move import (
    MoveRefused,
    claim_workspace_move,
    set_primary_computer,
)
from src.server.dependencies.usage_limits import (
    assert_provider_allowed,
    assert_spec_allowed,
    enforce_computer_limit,
    get_provider_allowances,
    platform_gating_active,
)
from src.server.models.computer import (
    CLAIMABLE_FOR_START,
    ComputerActionResponse,
    ComputerAlwaysOnRequest,
    ComputerCreate,
    ComputerListResponse,
    ComputerProviderOption,
    ComputerProvidersResponse,
    ComputerRenameRequest,
    ComputerResponse,
    ComputerSessionResponse,
    ComputerSpecRequest,
    ComputerStorageResponse,
)
from src.server.models.workspace_move import MakePrimaryResponse, WorkspaceMoveOut
from src.server.services.computer_disk import (
    STORAGE_BREAKDOWN_MAX_AGE_SECONDS,
    disk_from_row,
    storage_breakdown,
)
from src.server.services.computer_errors import (
    ComputerBusyError,
    ComputerRemovalRefused,
    MachineBusyError,
    ProviderNotOffered,
    UnknownResourceTier,
)
from src.server.services.spec_change import (
    SPEC_CHANGE_IN_PROGRESS,
    spec_change_from_row,
    spec_refusal,
)
from src.server.services.workspace_move import move_error_detail, move_from_row
from src.server.services.workspace_status_pubsub import (
    publish_computer_status_change,
    subscribe_to_computer_status,
)
from src.server.utils.api import CurrentUserId
from src.server.utils.error_sanitization import sandbox_unreachable_detail

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/computers", tags=["Computers"])


@contextlib.asynccontextmanager
async def _computer_quota_decision(user_id: str):
    """Serialize platform quota reads with the mutation that consumes the slot."""
    if not platform_gating_active():
        yield
        return
    async with computer_capacity_lock(user_id):
        yield


class ComputerRuntimeUnavailable(RuntimeError):
    """Workspace aliases fall back when the runtime is absent; computer routes return 503."""


def _computer_manager() -> Any:
    """Defer provider-stack imports so read routes remain loadable without the runtime."""
    try:
        from src.server.services.computer_manager import ComputerManager
    except ImportError as e:
        raise ComputerRuntimeUnavailable(str(e)) from None
    return ComputerManager.get_instance()


def _sandbox_config() -> Any:
    """The deployment's sandbox settings, the same the manager builds computers from."""
    return _computer_manager().config.sandbox


@contextlib.asynccontextmanager
async def _computer_action_errors(action: str, computer_id: str, *, spec: bool = False):
    """Let sandbox failures reach the app-level handler that owns their wording.

    A refusal the client can act on is a 409 in the spec change's error shape,
    the same one an accepted change's outcome carries. ``spec`` also names the
    unsaved files of an incomplete backup; elsewhere that stays a 400 sentence.
    """
    try:
        yield
    except HTTPException:
        raise
    except ComputerRuntimeUnavailable as e:
        logger.warning("Computer runtime unavailable for %s: %s", action, e)
        raise HTTPException(
            status_code=503, detail="Computer runtime is not available"
        ) from None
    except (SandboxGoneError, SandboxTransientError):
        raise
    except MachineBusyError as e:
        raise HTTPException(
            status_code=409, detail=spec_refusal("turn_active", str(e))
        ) from None
    except ComputerBusyError as e:
        raise HTTPException(status_code=409, detail=spec_refusal("busy", str(e))) from None
    except ComputerRemovalRefused as e:
        logger.warning("Refused to %s computer %s: %s", action, computer_id, e.code)
        raise HTTPException(
            status_code=409, detail={"code": e.code, "message": str(e)}
        ) from None
    except (ProviderNotOffered, UnknownResourceTier) as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except BackupIncomplete as e:
        # The ids and causes are the operator's; the person asking gets files.
        logger.warning(f"Refused to {action}: {e}")
        if spec:
            raise HTTPException(
                status_code=409,
                detail=spec_refusal("backup_incomplete", e.user_message, e.unsaved),
            ) from None
        raise HTTPException(status_code=400, detail=e.user_message) from None
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Error {action} computer {computer_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to {action} computer")


def require_computer_owner(computer: Optional[dict], *, user_id: str) -> None:
    if not computer:
        raise HTTPException(status_code=404, detail="Computer not found")
    if computer.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")


@contextlib.contextmanager
def move_refusals():
    """A move that cannot start answers in the shape its failure would be read in."""
    try:
        yield
    except MoveRefused as e:
        status = 404 if e.code == "not_found" else 409
        raise HTTPException(status_code=status, detail=move_error_detail(e.code)) from None


async def accept_workspace_move(
    user_id: str, workspace_id: str, computer_id: str, *, make_primary: bool = False
) -> WorkspaceMoveOut:
    """Claim a move and run it after answering; its outcome is read back from the row.

    Keyed by the move: the claim already refuses a second one for the user,
    and a move whose runner dies is finished by the sweep, not rerun here.
    """
    try:
        manager = _computer_manager()
    except ComputerRuntimeUnavailable as e:
        logger.warning("Computer runtime unavailable for a move: %s", e)
        raise HTTPException(
            status_code=503, detail="Computer runtime is not available"
        ) from None
    with move_refusals():
        move = await claim_workspace_move(
            user_id=user_id,
            workspace_id=workspace_id,
            target_computer_id=computer_id,
            make_primary=make_primary,
        )
    schedule_start(
        f"workspace-move:{move['move_id']}", lambda: manager.run_accepted_move(move)
    )
    logger.info(
        "Accepted move %s of workspace %s to computer %s for user %s",
        move["move_id"],
        workspace_id,
        computer_id,
        user_id,
    )
    return move_from_row(move)  # type: ignore[return-value]


def _spec_in_progress() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=spec_refusal("spec_in_progress", SPEC_CHANGE_IN_PROGRESS),
    )


def computer_to_response(
    computer: Dict[str, Any], *, owner: bool = True, workspace_count: int = 0
) -> ComputerResponse:
    """Withhold provider_ref from non-owners because it addresses a real billed machine."""
    return ComputerResponse(
        computer_id=str(computer["computer_id"]),
        user_id=computer["user_id"],
        kind=computer["kind"],
        name=computer.get("name") or "",
        status=computer["status"],
        resource_tier=computer.get("resource_tier") or "standard",
        is_always_on=bool(computer.get("is_always_on")),
        is_primary=bool(computer.get("is_primary")),
        root_dir=computer.get("root_dir") or "",
        workspace_count=workspace_count,
        layout_version=observed_layout_version(computer),
        provider_ref=computer.get("provider_ref") if owner else None,
        created_at=computer["created_at"],
        updated_at=computer["updated_at"],
        last_activity_at=computer.get("last_activity_at"),
        stopped_at=computer.get("stopped_at"),
        config=computer.get("config"),
        disk=disk_from_row(computer),
        spec_change=spec_change_from_row(computer),
    )


async def _owned_computer(computer_id: str, user_id: str) -> Dict[str, Any]:
    computer = await get_computer(computer_id)
    require_computer_owner(computer, user_id=user_id)
    return computer  # type: ignore[return-value]


async def _report_start_failure(computer_id: str, exc: Exception) -> None:
    """A background start behind 202 must publish failure or subscribers wait until timeout.

    Settle any surviving claim before publishing so clients re-reading on error
    see the settled row; the start's own revert normally owns this cleanup.
    """
    try:
        row = await get_computer(computer_id)
        if row is not None and row.get("status") == "starting":
            await update_computer_status(computer_id, "stopped", expected="starting")
    except Exception:
        logger.exception(
            "Could not settle computer %s after a failed start", computer_id
        )
    await publish_computer_status_change(
        computer_id, "error", extra={"error": sandbox_unreachable_detail(exc)}
    )


async def _run_computer_start(
    manager: Any, computer_id: str, on_start_claimed: Callable[[], None] | None = None
) -> None:
    """Do not report shutdown cancellation as failure; the start already reverts its claim."""
    try:
        if on_start_claimed is None:
            await manager.start_computer(computer_id)
        else:
            await manager.start_computer(computer_id, on_start_claimed=on_start_claimed)
    except Exception as exc:
        await _report_start_failure(computer_id, exc)
        raise


def schedule_computer_start(
    computer_id: str, on_start_claimed: Callable[[], None] | None = None
) -> asyncio.Task:
    manager = _computer_manager()
    return schedule_start(
        f"computer:{computer_id}",
        lambda: _run_computer_start(manager, computer_id, on_start_claimed),
    )


async def _schedule_claimed_start(computer_id: str) -> str:
    """Acknowledge only after the background owner has persisted its start claim."""
    claimed = asyncio.Event()
    task = schedule_computer_start(computer_id, claimed.set)
    waiter = asyncio.create_task(claimed.wait())
    try:
        while True:
            # A deduplicated task owns another request's callback. The durable
            # row lets every waiter observe its claim, including other workers.
            done, _ = await asyncio.wait(
                (task, waiter), timeout=0.1, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                try:
                    await task
                except HTTPException:
                    raise
                except Exception as exc:
                    raise HTTPException(
                        status_code=503, detail=sandbox_unreachable_detail(exc)
                    ) from None
            computer = await get_computer(computer_id)
            if computer is None:
                raise ValueError(f"Computer {computer_id} not found")
            if done or computer["status"] not in CLAIMABLE_FOR_START:
                return computer["status"]
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@router.post("", response_model=ComputerResponse, status_code=201)
async def create_computer(
    request: ComputerCreate,
    x_user_id: CurrentUserId,
):
    """Create a computer and prepare its sandbox in the background.

    Building a sandbox takes long enough that whoever first used the computer
    would wait through it, so it starts now and the client watches its events.
    Preparing holds a running slot from the insert, so the limit is asked here.
    """
    async with _computer_action_errors("create", "new"):
        sandbox = _sandbox_config()
        kind = request.kind or sandbox.provider
        await assert_provider_allowed(x_user_id, kind, default_kind=sandbox.provider)
        async with _computer_quota_decision(x_user_id):
            await assert_spec_allowed(x_user_id, request.resource_tier)
            await enforce_computer_limit(x_user_id)
            computer = await _computer_manager().create_computer_for_user(
                x_user_id,
                kind=kind,
                name=request.name,
                resource_tier=request.resource_tier,
                # The insert squashes this to FALSE when a live primary already
                # exists, so it reads as "primary if they have none". Without it a
                # user's first, named machine is not primary and their first project
                # mints a second one.
                is_primary=True,
                prepare=True,
            )
        computer_id = str(computer["computer_id"])
        status = await _schedule_claimed_start(computer_id)
        logger.info(
            "Created computer %s for user %s, preparing it (%s)",
            computer_id, x_user_id, status,
        )
        return computer_to_response({**computer, "status": status})


@router.get("", response_model=ComputerListResponse)
async def list_computers(x_user_id: CurrentUserId):
    rows = await get_computers_for_user(x_user_id)
    # One query for the whole list: stopping a machine takes every project on it
    # down, so the count is what the card needs to say so before asking.
    counts = await count_live_workspaces_by_computer(
        [str(row["computer_id"]) for row in rows]
    )
    return ComputerListResponse(
        computers=[
            computer_to_response(
                row, workspace_count=counts.get(str(row["computer_id"]), 0)
            )
            for row in rows
        ],
        total=len(rows),
    )


# Registered ahead of /{computer_id}, which would otherwise read "providers" as an id.
@router.get("/providers", response_model=ComputerProvidersResponse)
async def list_computer_providers(x_user_id: CurrentUserId):
    """The providers a new computer may be created on, and which the caller may pick."""
    try:
        sandbox = _sandbox_config()
    except ComputerRuntimeUnavailable as e:
        logger.warning("Computer runtime unavailable for providers: %s", e)
        raise HTTPException(
            status_code=503, detail="Computer runtime is not available"
        ) from None
    kinds = sandbox.creatable_providers()
    allowed = await get_provider_allowances(
        x_user_id, kinds, default_kind=sandbox.provider
    )
    return ComputerProvidersResponse(
        providers=[
            ComputerProviderOption(
                kind=kind, is_default=kind == sandbox.provider, allowed=allowed[kind]
            )
            for kind in kinds
        ]
    )


async def _counted_response(computer: Dict[str, Any]) -> ComputerResponse:
    """A single machine's response with its live project count.

    Every route that answers with a whole machine carries the count, including
    the ones that just changed it: a response is what the client replaces its
    row with, so one answering zero erases the number the card warns with.
    """
    computer_id = str(computer["computer_id"])
    counts = await count_live_workspaces_by_computer([computer_id])
    return computer_to_response(computer, workspace_count=counts.get(computer_id, 0))


@router.get("/{computer_id}", response_model=ComputerResponse)
async def get_computer_details(computer_id: str, x_user_id: CurrentUserId):
    return await _counted_response(await _owned_computer(computer_id, x_user_id))


@router.patch("/{computer_id}", response_model=ComputerResponse)
async def rename_computer(
    computer_id: str,
    request: ComputerRenameRequest,
    x_user_id: CurrentUserId,
):
    await _owned_computer(computer_id, x_user_id)
    updated = await db_rename_computer(computer_id, request.name)
    if updated is None:
        raise HTTPException(status_code=404, detail="Computer not found")
    return await _counted_response(updated)


@router.delete("/{computer_id}", status_code=204)
async def remove_computer(computer_id: str, x_user_id: CurrentUserId):
    """Remove a computer and its sandbox for good.

    Only an empty one other than the primary, which is where new workspaces
    go, so a removal never takes a workspace with it. A refusal the user can
    act on is a 409 whose ``code`` the client words.
    """
    async with _computer_action_errors("remove", computer_id):
        await _owned_computer(computer_id, x_user_id)
        await _computer_manager().remove_computer(computer_id)
        logger.info("Removed computer %s for user %s", computer_id, x_user_id)
    return Response(status_code=204)


@router.get("/{computer_id}/storage", response_model=ComputerStorageResponse)
async def get_computer_storage(computer_id: str, x_user_id: CurrentUserId):
    """Measure now when the machine runs; otherwise answer the last reading.

    A stopped machine is never started for this: the stored reading is what
    the warning already shows, and a folder breakdown is not worth a boot. A
    breakdown taken in the last minute is answered again: the panel asks on
    every open, and each measurement is a du over the whole root.
    """
    computer = await _owned_computer(computer_id, x_user_id)
    try:
        manager = _computer_manager()
    except ComputerRuntimeUnavailable:
        return ComputerStorageResponse(disk=disk_from_row(computer), live=False)
    if not manager.measures_disk(computer):
        return ComputerStorageResponse(disk=None, live=False)

    computer, sizes = await manager.refresh_computer_disk(
        computer,
        breakdown=True,
        breakdown_max_age_s=STORAGE_BREAKDOWN_MAX_AGE_SECONDS,
    )
    if sizes is None:
        return ComputerStorageResponse(disk=disk_from_row(computer), live=False)
    folders = await get_live_workspace_folders_for_computer(computer_id)
    workspaces, other = storage_breakdown(computer, folders, sizes)
    return ComputerStorageResponse(
        disk=disk_from_row(computer),
        workspaces=workspaces,
        other_bytes=other,
        live=True,
    )


@router.get("/{computer_id}/session", response_model=ComputerSessionResponse)
async def get_computer_session(computer_id: str, x_user_id: CurrentUserId):
    """Resolve sessions only when Postgres says running so this read cannot provision."""
    computer = await _owned_computer(computer_id, x_user_id)
    response = ComputerSessionResponse(
        computer_id=str(computer["computer_id"]),
        status=computer["status"],
        provider_ref=computer.get("provider_ref"),
    )
    if computer["status"] != "running":
        return response

    try:
        session = _computer_manager().cached_session_for_computer(computer)
    except ComputerRuntimeUnavailable:
        return response

    sandbox = getattr(session, "sandbox", None) if session is not None else None
    if sandbox is None:
        return response
    ref = getattr(sandbox, "sandbox_id", None)
    return response.model_copy(
        update={
            "ready": getattr(sandbox, "runtime", None) is not None,
            "session_provider_ref": ref if isinstance(ref, str) and ref else None,
        }
    )


@router.get("/{computer_id}/events")
async def computer_status_events(computer_id: str, x_user_id: CurrentUserId):
    computer = await _owned_computer(computer_id, x_user_id)

    def _frame(
        status: str,
        sandbox_state: str | None = None,
        error: str | None = None,
        stage: str | None = None,
    ) -> str:
        return sse_status_event(
            {"computer_id": computer_id}, status, sandbox_state, error, stage
        )

    async def _read_status() -> str | None:
        row = await get_computer(computer_id)
        return row["status"] if row else None

    return StreamingResponse(
        status_event_stream(
            initial_status=computer["status"],
            read_status=_read_status,
            subscribe=lambda: subscribe_to_computer_status(computer_id),
            frame=_frame,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/{computer_id}/start", response_model=ComputerActionResponse)
async def start_computer(
    computer_id: str,
    x_user_id: CurrentUserId,
    lazy: bool = Query(
        False,
        description=(
            "If true, schedule the start in the background and return 202 "
            "immediately with status='starting'. If false (default), block "
            "until the computer is up and return status='running'."
        ),
    ),
):
    async with _computer_action_errors("start", computer_id):
        manager = _computer_manager()
        computer = await _owned_computer(computer_id, x_user_id)
        if computer["status"] == "stopping":
            await manager.reconcile_stopping_computer(computer_id)
            computer = await _owned_computer(computer_id, x_user_id)
        status = computer["status"]

        if status == "running":
            return ComputerActionResponse(
                computer_id=computer_id,
                status=status,
                message=f"Computer is already {status}",
            )
        if status == "starting" and lazy:
            payload = ComputerActionResponse(
                computer_id=computer_id,
                status=status,
                message="Computer start already in progress",
            )
            return Response(
                status_code=202,
                content=payload.model_dump_json(),
                media_type="application/json",
            )
        if status not in CLAIMABLE_FOR_START and status != "starting":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot start computer in '{status}' state",
            )

        if lazy:
            status = await _schedule_claimed_start(computer_id)
            payload = ComputerActionResponse(
                computer_id=computer_id,
                status=status,
                message="Computer start initiated",
            )
            return Response(
                status_code=202,
                content=payload.model_dump_json(),
                media_type="application/json",
            )

        updated = await manager.start_computer(computer_id)
        return ComputerActionResponse(
            computer_id=computer_id,
            status=(updated or {}).get("status") or "running",
            message="Computer started successfully",
        )


@router.post("/{computer_id}/stop", response_model=ComputerActionResponse)
async def stop_computer(computer_id: str, x_user_id: CurrentUserId):
    async with _computer_action_errors("stop", computer_id):
        await _owned_computer(computer_id, x_user_id)
        updated = await _computer_manager().stop_computer(computer_id)
        logger.info(f"Stopped computer {computer_id}")
        return ComputerActionResponse(
            computer_id=computer_id,
            status=(updated or {}).get("status") or "stopped",
            message="Computer stopped successfully",
        )


@router.post("/{computer_id}/archive", response_model=ComputerActionResponse)
async def archive_computer(computer_id: str, x_user_id: CurrentUserId):
    async with _computer_action_errors("archive", computer_id):
        await _owned_computer(computer_id, x_user_id)
        updated = await _computer_manager().archive_computer(computer_id)
        logger.info(f"Archived computer {computer_id}")
        return ComputerActionResponse(
            computer_id=computer_id,
            status=(updated or {}).get("status") or "stopped",
            message="Computer archived successfully",
        )


@router.post(
    "/{computer_id}/spec", response_model=ComputerResponse, status_code=202
)
async def set_computer_spec(
    computer_id: str,
    request: ComputerSpecRequest,
    response: Response,
    x_user_id: CurrentUserId,
):
    """Accept a spec change and run it after answering; the outcome lands on ``spec_change``.

    A recreate takes minutes, longer than an edge proxy holds a request open,
    so the refusals a client can act on are answered here (403 off-plan, 429
    over quota, 409 ``turn_active``, ``spec_in_progress`` or ``busy``) and
    everything after is read back from the row. The current tier skips the
    count check so retries at quota remain valid, and answers 200 unchanged.
    """
    async with _computer_action_errors("set spec for", computer_id, spec=True):
        computer = await _owned_computer(computer_id, x_user_id)
        running = spec_change_from_row(computer)
        if running is not None and running.state == "in_progress":
            # Ahead of the same-tier shortcut: a running change persists its
            # target tier early, so a repeat would otherwise read as done.
            raise _spec_in_progress()
        manager = _computer_manager()
        if await manager.precheck_computer_spec(computer, request.tier):
            response.status_code = 200
            return await _counted_response(computer)
        if platform_gating_active():
            await assert_spec_allowed(
                x_user_id,
                request.tier,
                current_tier=computer.get("resource_tier") or "standard",
            )
        claimed = await manager.claim_computer_spec(computer_id, request.tier)
        if claimed is None:
            if await get_computer(computer_id) is None:
                raise HTTPException(status_code=404, detail="Computer not found")
            raise _spec_in_progress()
        claim_id = claimed["spec_change"]["claim_id"]
        # Keyed by the claim, not the machine: the row already refuses a second
        # fresh change, and a stale takeover must get a runner of its own
        # rather than be deduplicated onto the one it replaced.
        schedule_start(
            f"computer-spec:{computer_id}:{claim_id}",
            lambda: manager.run_accepted_spec_change(
                computer_id, request.tier, user_id=x_user_id, claim_id=claim_id
            ),
        )
        logger.info(
            "Accepted spec change of computer %s to %r for user %s",
            computer_id,
            request.tier,
            x_user_id,
        )
        return await _counted_response(claimed)


@router.post("/{computer_id}/make-primary", response_model=MakePrimaryResponse)
async def make_primary_computer(
    computer_id: str,
    response: Response,
    x_user_id: CurrentUserId,
):
    """Make this the computer new workspaces and Home land on.

    Home lives on the primary. With Home on no computer yet, or already on
    this one, the flip lands now (200). Otherwise Home's files move first,
    which outlasts a request: the move is accepted (202), the flip lands
    when it commits, and both are read back from Home's move.
    """
    async with _computer_action_errors("make primary", computer_id):
        await _owned_computer(computer_id, x_user_id)
        with move_refusals():
            flipped = await set_primary_computer(x_user_id, computer_id)
        if flipped:
            logger.info("Made computer %s primary for user %s", computer_id, x_user_id)
            return MakePrimaryResponse(computer_id=computer_id, is_primary=True)
        move = await accept_workspace_move(
            x_user_id, get_home_workspace_id(x_user_id), computer_id, make_primary=True
        )
        response.status_code = 202
        return MakePrimaryResponse(computer_id=computer_id, is_primary=False, move=move)


@router.post("/{computer_id}/always-on", response_model=ComputerResponse)
async def set_computer_always_on(
    computer_id: str,
    request: ComputerAlwaysOnRequest,
    x_user_id: CurrentUserId,
):
    """Disabling and idempotent enabling consume no slot and must not fail at quota.

    Start stopped machines in the background because always-on takes effect immediately.
    """
    async with _computer_action_errors("set always-on for", computer_id):
        await _owned_computer(computer_id, x_user_id)
        updated = await _computer_manager().set_computer_always_on(
            computer_id, request.enabled, user_id=x_user_id
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Computer not found")

        if request.enabled and updated.get("status") in CLAIMABLE_FOR_START:
            status = await _schedule_claimed_start(computer_id)
            logger.info(
                f"Always-on enabled: scheduled start for computer {computer_id}"
            )
            updated = {**updated, "status": status}

        logger.info(
            f"Set computer {computer_id} always-on to {request.enabled} "
            f"for user {x_user_id}"
        )
        return await _counted_response(updated)
