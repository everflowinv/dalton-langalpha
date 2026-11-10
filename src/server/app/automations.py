"""
Automations API Router.

Provides REST endpoints for creating, managing, and monitoring
scheduled automations (cron and one-time triggers).

Endpoints (/api/v1/automations):
- POST   /automations                              - Create automation
- GET    /automations                              - List automations
- GET    /automations/executions                   - Run feed across automations
- GET    /automations/delivery-options             - Chats an automation's runs can reach
- PUT    /automations/delivery-default             - Set a workspace's default chat
- GET    /automations/{automation_id}              - Get automation
- PATCH  /automations/{automation_id}              - Update automation
- DELETE /automations/{automation_id}              - Delete automation
- POST   /automations/{automation_id}/trigger      - Manual trigger
- POST   /automations/{automation_id}/pause        - Pause
- POST   /automations/{automation_id}/resume       - Resume
- GET    /automations/{automation_id}/executions    - Execution history
- POST   /automations/{automation_id}/executions/{execution_id}/skip
                                                   - Skip a waiting run
- POST   /automations/{automation_id}/executions/{execution_id}/dismiss
                                                   - Dismiss a failed run
"""

import logging
from typing import Any, Literal, Optional  # DALTON-HOOK(company-automations)
from uuid import UUID

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from src.dalton.company_identity import is_company_system_user  # DALTON-HOOK(company-automations)
from src.dalton.services.company_automations import (  # DALTON-HOOK(company-automations)
    AutomationHistoryResponse,
    automation_history_context,
    extract_expected_config_version,
)
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.database.home_workspace import is_home_row
from src.server.database.principal_reach import workspace_or_home
from src.server.database.workspace import get_workspace
from src.server.services.automations import lifecycle as handler
from src.server.services.automations import delivery
from src.server.models.automation import (
    AutomationCreate,
    AutomationReadResponse,
    AutomationResponse,
    AutomationRunResponse,
    AutomationRunsListResponse,
    AutomationsListResponse,
    AutomationUpdate,
    DeliveryDefaultUpdate,
)
from src.server.dependencies.principal import (
    ReadPrincipal,
    ReadUserId,
    RunUserId,
    WorkspaceSendPrincipal,
    WorkspaceWritePrincipal,
    WorkspaceWriteUserId,
    assert_automation_target,
)
from src.server.utils.api import (
    CurrentUserId,
    handle_api_exceptions,
    raise_not_found,
    require_workspace_owner,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Automations"])


def _refuse_company_service_actor(user_id: str) -> None:
    """The company system identity cannot call update, pause, or resume."""
    if is_company_system_user(user_id):
        raise HTTPException(status_code=403, detail="Forbidden")


def _company_write_response(row: dict[str, Any]):
    from src.dalton.services.company_agent_writes import company_agent_write_response

    return company_agent_write_response(row)


# =============================================================================
# CRUD Endpoints
# =============================================================================


@router.post("/automations", response_model=AutomationResponse, status_code=201)
@handle_api_exceptions("create automation", logger, conflict_on_value_error=True)
async def create_automation(
    request: AutomationCreate,
    principal: WorkspaceWritePrincipal,
    http_request: Request,
):
    """Create a new scheduled automation."""
    user_id = principal.user_id
    await assert_automation_target(
        principal,
        http_request,
        workspace_id=workspace_or_home(user_id, request.workspace_id),
        thread_id=request.conversation_thread_id,
    )
    try:
        automation = await handler.create_automation(user_id=user_id, data=request)
    except handler.DeliveryRefused as e:
        return _delivery_refused(e)
    resp = AutomationResponse.model_validate(automation)
    # DALTON-HOOK(dalton-notify): populate user-specific notify effective value and source
    from src.dalton.notify.dispatch import populate_automation_notify_state

    await populate_automation_notify_state(user_id, [resp])
    return resp


@router.get("/automations", response_model=AutomationsListResponse)
@handle_api_exceptions("list automations", logger)
async def list_automations(
    principal: ReadPrincipal,
    scope: Literal["personal", "research_tracking", "company"] | None = Query(
        None,
        description="Optional scope filter: personal, research_tracking, or company",
    ),
    status: str | None = Query(
        None,
        description=(
            "Filter by configuration status. Agent rows match automations.status "
            "exactly. Script rows match enabled and paused_by_user: paused when "
            "paused_by_user is true, active when enabled and not paused, disabled "
            "when not enabled and not paused. A run status such as succeeded is "
            "not a list filter."
        ),
    ),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List automations for the current user, each with its newest execution."""
    # DALTON-HOOK(company-automations): delegate explicit scope queries to AU1 read-only service
    if scope is not None:
        from src.dalton.services.company_automations import list_automations_for_scope

        return await list_automations_for_scope(
            principal,
            scope=scope,
            status=status,
            limit=limit,
            offset=offset,
        )
    user_id = principal.user_id
    automations, total = await auto_db.list_automations(
        principal.user_id,
        status=status,
        limit=limit,
        offset=offset,
        computer_id=principal.computer_id,
    )
    items = [AutomationResponse.model_validate(a) for a in automations]
    # DALTON-HOOK(dalton-notify): populate user-specific notify effective value and source
    from src.dalton.notify.dispatch import populate_automation_notify_state

    await populate_automation_notify_state(user_id, items)
    return AutomationsListResponse(
        automations=items,
        total=total,
    )


# Declared before /automations/{automation_id}, which would otherwise capture
# "executions" as an id.
@router.get("/automations/executions", response_model=AutomationRunsListResponse)
@handle_api_exceptions("list automation runs", logger)
async def list_automation_runs(
    principal: ReadPrincipal,
    scope: Literal["personal", "research_tracking", "company"] | None = Query(
        None,
        description="Optional scope filter: personal, research_tracking, or company",
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    thread_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(
        None,
        description=(
            "Exact stored execution status. Agent rows use pending, waiting, "
            "running, completed, failed, timeout, or skipped. Script rows use "
            "queued, starting, running, succeeded, failed, timed_out, aborted, "
            "skipped_low_memory, skipped_superseded, or cancelled. succeeded "
            "does not match completed, and a thread_id matches only a real "
            "agent thread."
        ),
    ),
):
    """List executions. Omitted scope stays the caller's own agent runs."""
    # DALTON-HOOK(company-automations): company feed merges agent runs and script runs
    thread_filter = str(thread_id) if thread_id else None
    if scope == "company":
        from src.dalton.services.company_task_read import list_company_execution_feed

        return await list_company_execution_feed(
            principal,
            thread_id=thread_filter,
            status=status,
            limit=limit,
            offset=offset,
        )
    if scope is not None:
        filters: dict[str, Any] = {
            "thread_id": thread_filter,
            "status": status,
            "limit": limit,
            "offset": offset,
            "scope": scope,
        }
        if principal.computer_id is not None:
            filters["computer_id"] = principal.computer_id
        return await _runs_page(principal.user_id, **filters)
    if principal.computer_id is not None:
        return await _runs_page(
            principal.user_id,
            thread_id=thread_filter,
            status=status,
            limit=limit,
            offset=offset,
            computer_id=principal.computer_id,
        )
    # DALTON-HOOK(company-automations): retain the principal owner for the default execution feed
    return await _runs_page(
        principal.user_id,
        thread_id=thread_filter,
        status=status,
        limit=limit,
        offset=offset,
    )


# =============================================================================
# Delivery through the messaging service
# =============================================================================


def _delivery_refused(e: handler.DeliveryRefused) -> JSONResponse:
    """A write refused for its delivery: the 409 it has always been, with the
    sentence in ``detail`` and each entry's reason in ``problems``."""
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(e),
            "problems": [{"entry": entry, "message": why} for entry, why in e.refusals],
        },
    )


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=503, detail=detail)


async def _delivery_workspace(user_id: str, workspace_id: Optional[UUID]) -> Optional[str]:
    """The workspace whose default chat an entry naming only an app follows,
    once the user is checked as its owner. None without one, and for Home,
    which is none of the user's workspaces: its runs deliver as a run with no
    workspace does."""
    if workspace_id is None:
        return None
    workspace = await get_workspace(str(workspace_id))
    require_workspace_owner(workspace, user_id=user_id)
    return None if is_home_row(workspace) else str(workspace_id)


# Declared before /automations/{automation_id}, which would otherwise capture
# the path as an id.
@router.get("/automations/delivery-options")
@handle_api_exceptions("list delivery options", logger)
async def get_delivery_options(
    user_id: ReadUserId,
    workspace_id: Optional[UUID] = Query(None),
) -> dict[str, Any]:
    """The chats each linked app offers an automation's runs, and the one an
    entry naming only the app reaches. Asked with the automation's workspace,
    or with none for one that has none. ``workspace_id`` is the workspace the
    answer was made for: null without one and for Home, which has no default
    chat. ``enabled`` is false, with no apps, on a server with no messaging
    service."""
    workspace = await _delivery_workspace(user_id, workspace_id)
    try:
        return await delivery.options(user_id, workspace)
    except delivery.Unavailable as e:
        raise _unavailable(str(e))


@router.put("/automations/delivery-default")
@handle_api_exceptions("set delivery default", logger)
async def set_delivery_default(
    request: DeliveryDefaultUpdate,
    user_id: CurrentUserId,
):
    """Set the chat on one app that a workspace's automations deliver to when
    an entry names only the app, or clear it with a null address. Answers
    the chat as saved. Home has none to set."""
    workspace = await _delivery_workspace(user_id, request.workspace_id)
    try:
        return await delivery.set_default(
            user_id, workspace, request.platform, request.address
        )
    except delivery.Refused as e:
        return JSONResponse(
            status_code=400, content={"detail": str(e), "problems": e.problems}
        )
    except delivery.NoService as e:
        raise HTTPException(status_code=404, detail=str(e))
    except delivery.Busy as e:
        raise HTTPException(status_code=409, detail=str(e))
    except delivery.Unavailable as e:
        raise _unavailable(str(e))


@router.get("/automations/{automation_id}", response_model=AutomationReadResponse)
@handle_api_exceptions("get automation", logger)
async def get_automation(
    automation_id: str,
    principal: ReadPrincipal,
):
    """Get one automation by UUID, or one manifest script by ``data:<job_key>``."""
    # DALTON-HOOK(company-automations): parse the id, then apply owner or company access
    parsed = _parse_read_id(automation_id)
    if parsed.kind == "script":
        from src.dalton.services.company_task_read import get_script_automation

        return await get_script_automation(principal, str(parsed.job_key))
    # DALTON-HOOK(company-automations): a computer token's detail uses the same run reach as the list
    detail_kwargs: dict[str, Any] = {}
    if principal.computer_id is not None:
        detail_kwargs["computer_id"] = principal.computer_id
    automation = await auto_db.get_automation(
        str(parsed.agent_id), principal.user_id, **detail_kwargs
    )
    if automation:
        resp = AutomationResponse.model_validate(automation)
        # DALTON-HOOK(dalton-notify): populate user-specific notify effective value and source
        from src.dalton.notify.dispatch import populate_automation_notify_state

        await populate_automation_notify_state(principal.user_id, [resp])
        return resp
    from src.dalton.database import company_data_tasks as company_data_db
    from src.dalton.services.company_automations import (
        apply_company_notify_representation,
        require_company_scope_read_access,
    )
    from src.dalton.services.company_task_read import scrub_company_agent_row

    company_row = await company_data_db.get_company_agent_detail(str(parsed.agent_id))
    if company_row is None:
        raise_not_found("Automation")
    await require_company_scope_read_access(principal)
    if company_row.get("deleted_at") is not None:
        raise_not_found("Automation")
    company_resp = AutomationResponse.model_validate(scrub_company_agent_row(company_row))
    apply_company_notify_representation(company_resp, company_row)
    return company_resp


@router.patch("/automations/{automation_id}", response_model=AutomationResponse)
@handle_api_exceptions("update automation", logger, conflict_on_value_error=True)
async def update_automation(
    automation_id: UUID,
    request: AutomationUpdate,
    principal: WorkspaceSendPrincipal,
    http_request: Request,
):
    """Partial update of an automation."""
    user_id = principal.user_id
    # DALTON-HOOK(company-automations): company system identity cannot write through this route
    _refuse_company_service_actor(user_id)
    await assert_automation_target(
        principal,
        http_request,
        workspace_id=request.workspace_id,
        thread_id=request.conversation_thread_id,
    )
    try:
        # DALTON-HOOK(company-automations): validate optional expected_config_version and propagate via context
        expected_cv = extract_expected_config_version(request)
        fields = request.model_dump(exclude_none=True)
        # DALTON-HOOK(automation-model): allow explicit null llm_model to clear model override
        if "llm_model" in request.model_fields_set and request.llm_model is None:
            fields["llm_model"] = None
        with automation_history_context(
            actor_user_id=user_id,
            action="update",
            expected_config_version=expected_cv,
        ):
            automation = await handler.update_automation(
                automation_id=str(automation_id),
                user_id=user_id,
                fields=fields,
            )
    except handler.DeliveryRefused as e:
        return _delivery_refused(e)
    except ValidationError as e:
        # The body parsed, then failed against the stored kind of trigger:
        # a 422 shaped like the one FastAPI answers a bad body with.
        raise HTTPException(
            status_code=422,
            detail=e.errors(include_url=False, include_context=False, include_input=False),
        )
    if automation:
        resp = AutomationResponse.model_validate(automation)
        # DALTON-HOOK(dalton-notify): populate user-specific notify effective value and source
        from src.dalton.notify.dispatch import populate_automation_notify_state

        await populate_automation_notify_state(user_id, [resp])
        return resp
    # DALTON-HOOK(company-automations): a miss on the caller may be an existing company row
    from src.dalton.services.company_agent_writes import update_company_agent

    company_row = await update_company_agent(
        principal,
        str(automation_id),
        fields,
        request=request,
        expected_config_version=expected_cv,
    )
    if not company_row:
        raise_not_found("Automation")
    return _company_write_response(company_row)


@router.delete("/automations/{automation_id}", status_code=204)
@handle_api_exceptions("delete automation", logger)
async def delete_automation(
    automation_id: UUID,
    user_id: WorkspaceWriteUserId,
):
    """Delete an automation (cascade deletes executions)."""
    deleted = await auto_db.delete_automation(str(automation_id), user_id)
    if not deleted:
        raise_not_found("Automation")
    return Response(status_code=204)


# =============================================================================
# Control Endpoints
# =============================================================================


@router.post("/automations/{automation_id}/trigger")
@handle_api_exceptions("trigger automation", logger, conflict_on_value_error=True)
async def trigger_automation(
    automation_id: UUID,
    user_id: RunUserId,
):
    """Manually trigger an automation immediately (doesn't affect next_run_at)."""
    result = await handler.trigger_automation(str(automation_id), user_id)
    return result


@router.post("/automations/{automation_id}/pause", response_model=AutomationResponse)
@handle_api_exceptions("pause automation", logger, conflict_on_value_error=True)
async def pause_automation(
    automation_id: UUID,
    principal: WorkspaceWritePrincipal,
    body: Any = Body(default=None),
):
    """Pause an active automation."""
    user_id = principal.user_id
    # DALTON-HOOK(company-automations): company system identity cannot pause through this route
    _refuse_company_service_actor(user_id)
    # DALTON-HOOK(company-automations): validate optional expected_config_version on pause
    expected_cv = extract_expected_config_version(body)
    with automation_history_context(
        actor_user_id=user_id,
        action="pause",
        expected_config_version=expected_cv,
    ):
        automation = await handler.pause_automation(str(automation_id), user_id)
    if automation:
        resp = AutomationResponse.model_validate(automation)
        # DALTON-HOOK(dalton-notify): populate user-specific notify effective value and source
        from src.dalton.notify.dispatch import populate_automation_notify_state

        await populate_automation_notify_state(user_id, [resp])
        return resp
    # DALTON-HOOK(company-automations): a miss on the caller may be an existing company row
    from src.dalton.services.company_agent_writes import pause_company_agent

    company_row = await pause_company_agent(
        principal,
        str(automation_id),
        expected_config_version=expected_cv,
    )
    if not company_row:
        raise_not_found("Automation")
    return _company_write_response(company_row)


@router.post("/automations/{automation_id}/resume", response_model=AutomationResponse)
@handle_api_exceptions("resume automation", logger, conflict_on_value_error=True)
async def resume_automation(
    automation_id: UUID,
    # Re-arms runs into the automation's thread, so not the call's own.
    principal: WorkspaceSendPrincipal,
    body: Any = Body(default=None),
):
    """Resume a paused or disabled automation."""
    user_id = principal.user_id
    # DALTON-HOOK(company-automations): company system identity cannot resume through this route
    _refuse_company_service_actor(user_id)
    # DALTON-HOOK(company-automations): validate optional expected_config_version on resume
    expected_cv = extract_expected_config_version(body)
    with automation_history_context(
        actor_user_id=user_id,
        action="resume",
        expected_config_version=expected_cv,
    ):
        automation = await handler.resume_automation(str(automation_id), user_id)
    if automation:
        resp = AutomationResponse.model_validate(automation)
        # DALTON-HOOK(dalton-notify): populate user-specific notify effective value and source
        from src.dalton.notify.dispatch import populate_automation_notify_state

        await populate_automation_notify_state(user_id, [resp])
        return resp
    # DALTON-HOOK(company-automations): a miss on the caller may be an existing company row
    from src.dalton.services.company_agent_writes import resume_company_agent

    company_row = await resume_company_agent(
        principal,
        str(automation_id),
        expected_config_version=expected_cv,
    )
    if not company_row:
        raise_not_found("Automation")
    return _company_write_response(company_row)


@router.post("/automations/{automation_id}/executions/{execution_id}/skip")
@handle_api_exceptions("skip execution", logger, conflict_on_value_error=True)
async def skip_execution(
    automation_id: UUID,
    execution_id: UUID,
    user_id: WorkspaceWriteUserId,
):
    """Skip a run that is waiting for its thread's current turn to end."""
    result = await handler.skip_execution(
        str(automation_id), str(execution_id), user_id
    )
    if result is None:
        raise_not_found("Automation")
    return result


@router.post(
    "/automations/{automation_id}/executions/{execution_id}/dismiss",
    response_model=AutomationResponse,
)
@handle_api_exceptions("dismiss execution", logger, conflict_on_value_error=True)
async def dismiss_execution(
    automation_id: UUID,
    execution_id: UUID,
    user_id: WorkspaceWriteUserId,
):
    """Take a failed run out of Needs attention until another run fails."""
    automation = await handler.dismiss_execution(
        str(automation_id), str(execution_id), user_id
    )
    if not automation:
        raise_not_found("Automation")
    resp = AutomationResponse.model_validate(automation)
    # DALTON-HOOK(dalton-notify): populate user-specific notify effective value and source
    from src.dalton.notify.dispatch import populate_automation_notify_state

    await populate_automation_notify_state(user_id, [resp])
    return resp


# =============================================================================
# Execution History
# =============================================================================


@router.get(
    "/automations/{automation_id}/executions",
    response_model=AutomationRunsListResponse,
)
@handle_api_exceptions("list executions", logger)
async def list_executions(
    automation_id: str,
    principal: ReadPrincipal,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List execution history for one agent or one manifest script."""
    # DALTON-HOOK(company-automations): company rows are shared; other rows stay owner-scoped
    parsed = _parse_read_id(automation_id)
    if parsed.kind == "script":
        from src.dalton.services.company_task_read import list_script_executions

        return await list_script_executions(
            principal,
            str(parsed.job_key),
            limit=limit,
            offset=offset,
        )
    from src.dalton.database import company_data_tasks as company_data_db
    from src.dalton.services.company_task_read import (
        is_shared_company_agent,
        list_company_agent_executions,
    )

    identity = await company_data_db.get_automation_owner_identity(str(parsed.agent_id))
    if is_shared_company_agent(identity):
        from src.dalton.services.company_automations import (
            require_company_scope_read_access,
        )

        await require_company_scope_read_access(principal)
        if identity is not None and identity.get("deleted_at") is not None:
            raise_not_found("Automation")
        return await list_company_agent_executions(
            principal,
            str(parsed.agent_id),
            limit=limit,
            offset=offset,
        )
    # DALTON-HOOK(company-automations): a computer token uses the same execution reach as the feed
    filters: dict[str, Any] = {
        "automation_id": str(parsed.agent_id),
        "limit": limit,
        "offset": offset,
    }
    if principal.computer_id is not None:
        filters["computer_id"] = principal.computer_id
    return await _runs_page(principal.user_id, **filters)


# DALTON-HOOK(company-automations): read-only modification history endpoint (AU1 §3.5)
@router.get(
    "/automations/{automation_id}/history",
    response_model=AutomationHistoryResponse,
)
@handle_api_exceptions("list automation modification history", logger)
async def list_automation_history(
    automation_id: str,
    principal: ReadPrincipal,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List stored version history for an agent UUID or a ``data:<job_key>``."""
    from src.dalton.services.company_automations import (
        get_automation_modification_history,
    )

    return await get_automation_modification_history(
        principal,
        str(automation_id),
        limit=limit,
        offset=offset,
    )


def _parse_read_id(automation_id: str):
    """Return a parsed read id, or 422 before any database call."""
    from src.dalton.services.company_task_read import (
        InvalidAutomationId,
        parse_automation_read_id,
    )

    # DALTON-HOOK(company-automations): normalize public task identifiers before reads
    try:
        return parse_automation_read_id(automation_id)
    except InvalidAutomationId as exc:
        raise HTTPException(status_code=422, detail="Invalid automation id") from exc


async def _runs_page(user_id: str, **filters) -> AutomationRunsListResponse:
    """The feed and one automation's history: one query, one page shape."""
    executions, has_more = await exec_db.list_executions(user_id, **filters)
    return AutomationRunsListResponse(
        executions=[AutomationRunResponse.model_validate(e) for e in executions],
        has_more=has_more,
    )
