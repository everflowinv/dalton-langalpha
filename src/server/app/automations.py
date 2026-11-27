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
- DELETE /automations/{automation_id}              - Soft-delete into the recycle bin
- POST   /automations/{automation_id}/restore      - Restore from the recycle bin
- POST   /automations/{automation_id}/rollback     - Roll back one configuration version
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
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from src.dalton.company_identity import is_company_system_user  # DALTON-HOOK(company-automations)
from src.dalton.services.company_automations import (  # DALTON-HOOK(company-automations)
    AutomationHistoryResponse,
    automation_history_context,
    extract_expected_config_version,
    validate_expected_config_version,
)
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.database.home_workspace import is_home_row
from src.server.database.workspace import get_workspace
from src.server.services.automations import lifecycle as handler
from src.server.services.automations import delivery
from src.server.models.automation import (
    AutomationCreate,
    AutomationReadResponse,
    AutomationResponse,
    AutomationRollbackRequest,
    AutomationRunResponse,
    AutomationRunsListResponse,
    AutomationsListResponse,
    AutomationUpdate,
    CompanyScriptUpdate,
    CompanyScriptVersionRequest,
    DeliveryDefaultUpdate,
    RecycleVersionRequest,
)
from src.server.dependencies import cli_approval
from src.server.dependencies.principal import (
    ReadPrincipal,
    ReadUserId,
    RunPrincipal,
    WorkspaceDeletePrincipal,
    WorkspaceSendPrincipal,
    WorkspaceWritePrincipal,
    WorkspaceWriteUserId,
    uuid_or_none,
)
from src.server.utils.api import (
    CurrentUserId,
    handle_api_exceptions,
    raise_not_found,
    require_workspace_owner,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Automations"])


# DALTON-HOOK(company-automations): company changes record a human caller and return public task fields.
def _refuse_company_service_actor(user_id: str) -> None:
    """The company system identity cannot call update, pause, or resume."""
    if is_company_system_user(user_id):
        raise HTTPException(status_code=403, detail="Forbidden")


def _company_write_response(row: dict[str, Any]):
    from src.dalton.services.company_agent_writes import company_agent_write_response

    return company_agent_write_response(row)


def _body_is_missing(body: Any) -> bool:
    return body is None or type(body).__name__ in ("Body", "FieldInfo")


def _body_validation_error(exc: ValidationError) -> HTTPException:
    """422 in the same shape FastAPI uses for a rejected JSON body."""
    detail = []
    for item in exc.errors(include_url=False, include_context=False, include_input=False):
        loc = tuple(item.get("loc") or ())
        if not loc or loc[0] != "body":
            item = {**item, "loc": ("body", *loc)}
        detail.append(item)
    return HTTPException(status_code=422, detail=detail)


def _validate_model(model: Any, body: Any) -> Any:
    # DALTON-HOOK(company-automations): choose the model after the id is classified
    if _body_is_missing(body):
        raise HTTPException(status_code=422, detail="Request body is required")
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise _body_validation_error(exc) from None


def _script_expected_version(body: Any) -> int:
    parsed = _validate_model(CompanyScriptVersionRequest, body)
    return int(parsed.expected_config_version)


# =============================================================================
# CRUD Endpoints
# =============================================================================


# DALTON-HOOK(report-delivery): map ReportDeliveryError to HTTP status codes for REST endpoints
def _report_delivery_http_error(exc: Exception) -> HTTPException:
    code = str(getattr(exc, "code", "") or "")
    detail = str(getattr(exc, "message", None) or str(exc))
    if code in {
        "forbidden",
        "rest_group_target_forbidden",
        "unauthorized_group",
        "not_group_member",
        "admin_only_channel",
        "workspace_forbidden",
        "dm_not_bound",
        "feishu_not_bound",
        "owner_tier_forbidden",
        "confirmation_required",
        "confirmation_invalid",
        "web_session_required",
    }:
        return HTTPException(status_code=403, detail=detail)
    if code in {"not_found", "forbidden_or_not_found"}:
        return HTTPException(status_code=404, detail=detail)
    if code == "conflict":
        return HTTPException(status_code=409, detail=detail)
    return HTTPException(status_code=400, detail=detail)


@router.post("/automations", response_model=AutomationResponse, status_code=201)
@handle_api_exceptions("create automation", logger, conflict_on_value_error=True)
async def create_automation(
    request: AutomationCreate,
    principal: WorkspaceSendPrincipal,
    http_request: Request,
):
    """Create a new scheduled automation."""
    user_id = principal.user_id
    await cli_approval.automation_runs(http_request, principal, "create", body=request)
    # DALTON-HOOK(report-delivery): enforce unified report_profile rules & atomic route sync on REST create when Dalton is enabled
    from src.dalton.settings import is_dalton_enabled

    dalton_enabled = is_dalton_enabled()
    report_profile_plan = None
    if dalton_enabled and isinstance(request.metadata, dict) and "report_profile" in request.metadata:
        from src.dalton.services.report_delivery import (
            ReportDeliveryError,
            execute_report_profile_rest_mutation,
            prepare_rest_report_profile_write,
        )

        try:
            report_profile_plan = await prepare_rest_report_profile_write(
                principal=principal,
                http_request=http_request,
                automation={
                    "user_id": user_id,
                    "name": request.name,
                    "instruction": request.instruction,
                    "metadata": {},
                },
                new_profile=request.metadata.get("report_profile"),
                action="create",
                after_metadata=request.metadata,
                body_confirmation_token=request.report_delivery_confirmation_token,
            )
        except ReportDeliveryError as exc:
            raise _report_delivery_http_error(exc) from None
    route_warning: str | None = None
    try:
        if report_profile_plan is not None:
            # DALTON-HOOK(report-delivery): persist normalized after_metadata on create, matching update
            create_data = request.model_copy(
                update={"metadata": report_profile_plan.change.after_metadata}
            )
            try:
                automation, route_warning = await execute_report_profile_rest_mutation(
                    change=report_profile_plan.change,
                    actor_user_id=user_id,
                    confirmation_token=report_profile_plan.confirmation_token,
                    confirmation_action=report_profile_plan.confirmation_action,
                    confirmation_automation_id=report_profile_plan.confirmation_automation_id,
                    confirmation_config_version=report_profile_plan.confirmation_config_version,
                    mutate_fn=lambda db_conn: handler.create_automation(
                        user_id=user_id, data=create_data, conn=db_conn
                    ),
                )
            except ReportDeliveryError as exc:
                raise _report_delivery_http_error(exc) from None
        else:
            automation = await handler.create_automation(user_id=user_id, data=request)
    except handler.DeliveryRefused as e:
        return _delivery_refused(e)
    except handler.AutomationRefusal as e:
        # DALTON-HOOK(automation-effort): map invalid reasoning_effort refusals to HTTP 422
        if e.field == "reasoning_effort":
            raise HTTPException(status_code=422, detail=str(e)) from None
        raise
    resp = AutomationResponse.model_validate(automation)
    # DALTON-HOOK(dalton-notify): populate user-specific notify effective value and source
    from src.dalton.notify.dispatch import populate_automation_notify_state

    await populate_automation_notify_state(user_id, [resp])
    # DALTON-HOOK(report-delivery): warn when a newly created report-skill automation has no report_profile or unsynced route
    if dalton_enabled:
        from src.dalton.services.report_delivery import build_missing_report_profile_warning

        resp.report_delivery_warning = build_missing_report_profile_warning(
            resp.instruction, resp.metadata
        )
        resp.route_warning = route_warning
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
    include_deleted: bool = Query(
        False,
        description="When true, return only this group's soft-deleted rows",
    ),
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
            include_deleted=include_deleted,
        )
    user_id = principal.user_id
    automations, total = await auto_db.list_automations(
        principal.user_id,
        status=status,
        limit=limit,
        offset=offset,
        include_deleted=include_deleted,
    )
    items = [AutomationResponse.model_validate(a) for a in automations]
    # DALTON-HOOK(dalton-notify): populate user-specific notify effective value and source
    from src.dalton.notify.dispatch import populate_automation_notify_state
    # DALTON-HOOK(execution-delivery-status): attach delivery_summary to each automation's last_execution
    from src.dalton.services.execution_delivery_status import (
        enrich_automations_with_delivery_summary,
    )

    await populate_automation_notify_state(user_id, items)
    await enrich_automations_with_delivery_summary(items)
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
    wait_reason: Literal["thread_busy", "computer_unreachable"] | None = Query(None),
):
    """List executions. Omitted scope stays the caller's own agent runs.

    ``wait_reason`` narrows on the server, before the page is cut, so runs
    waiting for one reason cannot push those waiting for the other off it.
    """
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
            "wait_reason": wait_reason,
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
            wait_reason=wait_reason,
            limit=limit,
            offset=offset,
            computer_id=principal.computer_id,
        )
    # DALTON-HOOK(company-automations): retain the principal owner for the default execution feed
    return await _runs_page(
        principal.user_id,
        thread_id=thread_filter,
        status=status,
        wait_reason=wait_reason,
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
    # DALTON-HOOK(coverage-units): allow delivery workspace with web write access only when units & auth enabled
    from src.dalton.settings import (
        is_coverage_units_enabled,
        is_dalton_supabase_auth_enabled,
    )

    ws_owner = str((workspace or {}).get("user_id") or "").strip()
    colleague_write = (
        workspace is not None
        and ws_owner != str(user_id)
        and is_coverage_units_enabled()
        and is_dalton_supabase_auth_enabled()
    )
    if colleague_write:
        from src.dalton.sharing import (
            ColleagueAccessRevokedError,
            require_workspace_access,
        )

        try:
            await require_workspace_access(
                str(workspace_id),
                user_id,
                channel="web",
                need="write",
                workspace=workspace,
            )
        except ColleagueAccessRevokedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    else:
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
        # DALTON-HOOK(execution-delivery-status): attach delivery_summary to detail last_execution
        from src.dalton.services.execution_delivery_status import (
            enrich_automations_with_delivery_summary,
        )

        await populate_automation_notify_state(principal.user_id, [resp])
        await enrich_automations_with_delivery_summary([resp])
        return resp
    from src.dalton.database import company_data_tasks as company_data_db
    from src.dalton.services.company_automations import (
        apply_company_notify_representation,
        require_company_scope_read_access,
    )
    from src.dalton.services.company_task_read import scrub_company_agent_row
    from src.dalton.services.execution_delivery_status import (
        enrich_automations_with_delivery_summary,
    )

    company_row = await company_data_db.get_company_agent_detail(str(parsed.agent_id))
    if company_row is None:
        raise_not_found("Automation")
    await require_company_scope_read_access(principal)
    if company_row.get("deleted_at") is not None:
        raise_not_found("Automation")
    company_resp = AutomationResponse.model_validate(scrub_company_agent_row(company_row))
    apply_company_notify_representation(company_resp, company_row)
    # DALTON-HOOK(execution-delivery-status): attach delivery_summary to company agent detail last_execution
    await enrich_automations_with_delivery_summary([company_resp])
    return company_resp


# DALTON-HOOK(company-automations): UUID keeps the agent write; data:<job_key> updates the script
@router.patch("/automations/{automation_id}", response_model=AutomationReadResponse)
@handle_api_exceptions("update automation", logger, conflict_on_value_error=True)
async def update_automation(
    automation_id: str,
    principal: WorkspaceSendPrincipal,
    http_request: Request,
    body: Any = Body(...),
):
    """Partial update of an automation."""
    # DALTON-HOOK(company-automations): classify the id before choosing the agent or script body
    parsed = _parse_read_id(automation_id)
    if parsed.kind == "script":
        script_request = _validate_model(CompanyScriptUpdate, body)
        from src.dalton.services.company_script_writes import update_company_script

        return await update_company_script(
            principal, str(parsed.job_key), script_request
        )
    # Former UUID path parameter normalized the text. Keep that canonical form.
    agent_id = str(parsed.agent_id)
    request = _validate_model(AutomationUpdate, body)
    user_id = principal.user_id
    # DALTON-HOOK(company-automations): company system identity cannot write through this route
    _refuse_company_service_actor(user_id)
    await cli_approval.automation_runs(
        http_request, principal, "edit", automation_id=agent_id, body=request
    )
    # DALTON-HOOK(report-delivery): validate REST metadata.report_profile changes via shared rule when Dalton is enabled
    from src.dalton.settings import is_dalton_enabled

    dalton_enabled = is_dalton_enabled()
    report_profile_plan = None
    existing_is_company = False
    unchanged_rp_observed_cv: int | None = None
    if dalton_enabled and request.metadata is not None:
        from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID, is_company_system_user

        existing_row = await auto_db.get_automation(agent_id, user_id)
        if existing_row is None:
            existing_row = await auto_db.get_automation(agent_id, COMPANY_SYSTEM_USER_ID)
        if existing_row is None:
            # Allow admins updating another user's automation to hit the same report_profile guard.
            from src.dalton.services.report_delivery import _load_automation_for_actor, get_user_tier

            if await get_user_tier(user_id) == "admin":
                try:
                    existing_row = await _load_automation_for_actor(agent_id, user_id, "admin")
                except Exception:
                    existing_row = None
        if existing_row is None:
            if "report_profile" in request.metadata:
                raise_not_found("Automation")
        else:
            existing_is_company = existing_row.get("scope") == "company" or is_company_system_user(
                existing_row.get("user_id")
            )
            old_meta = existing_row.get("metadata") or {}
            old_rp = old_meta.get("report_profile") if isinstance(old_meta, dict) else None
            new_rp = request.metadata.get("report_profile")
            has_rp = ("report_profile" in request.metadata) or (
                isinstance(old_meta, dict) and "report_profile" in old_meta
            )
            if has_rp and new_rp != old_rp:
                from src.dalton.services.report_delivery import (
                    ReportDeliveryError,
                    prepare_rest_report_profile_write,
                )

                target_view = dict(existing_row)
                if request.instruction is not None:
                    target_view["instruction"] = request.instruction
                try:
                    report_profile_plan = await prepare_rest_report_profile_write(
                        principal=principal,
                        http_request=http_request,
                        automation=target_view,
                        new_profile=new_rp,
                        action="update",
                        after_metadata=request.metadata,
                        body_confirmation_token=request.report_delivery_confirmation_token,
                        expected_config_version=extract_expected_config_version(request),
                    )
                except ReportDeliveryError as exc:
                    raise _report_delivery_http_error(exc) from None
            elif has_rp:
                # Bind the unlocked read version as a write condition so a concurrent
                # clear/replace of report_profile cannot be overwritten without confirmation.
                # An explicit expected_config_version MUST match the version at which
                # report_profile was observed unchanged; otherwise fail closed with 409.
                raw_cv = existing_row.get("config_version")
                if isinstance(raw_cv, int) and not isinstance(raw_cv, bool):
                    explicit_cv = extract_expected_config_version(request)
                    if explicit_cv is not None and explicit_cv != raw_cv:
                        raise HTTPException(
                            status_code=409,
                            detail="任务配置版本已变更，请刷新后重试。",
                        )
                    unchanged_rp_observed_cv = raw_cv
    route_warning: str | None = None
    try:
        # DALTON-HOOK(company-automations): validate optional expected_config_version and propagate via context
        expected_cv = extract_expected_config_version(request)
        if unchanged_rp_observed_cv is not None:
            expected_cv = unchanged_rp_observed_cv
        fields = request.model_dump(exclude_none=True)
        # DALTON-HOOK(report-delivery): never persist an explicit null report_profile key
        if (
            report_profile_plan is None
            and isinstance(fields.get("metadata"), dict)
            and "report_profile" in fields["metadata"]
            and fields["metadata"].get("report_profile") is None
        ):
            cleaned_meta = dict(fields["metadata"])
            cleaned_meta.pop("report_profile", None)
            fields["metadata"] = cleaned_meta
        # DALTON-HOOK(automation-model): allow explicit null llm_model to clear model override
        if "llm_model" in request.model_fields_set and request.llm_model is None:
            fields["llm_model"] = None
        # DALTON-HOOK(automation-effort): allow explicit null reasoning_effort to clear effort override
        if "reasoning_effort" in request.model_fields_set and request.reasoning_effort is None:
            fields["reasoning_effort"] = None
        if report_profile_plan is not None:
            # DALTON-HOOK(report-delivery): keep profile, one-time confirmation consume, route sync, audit history, and notify slot in one transaction
            from src.dalton.services.report_delivery import (
                ReportDeliveryError,
                execute_report_profile_rest_mutation,
            )

            eff_expected_cv = (
                expected_cv
                if expected_cv is not None
                else report_profile_plan.required_expected_config_version
            )
            fields["metadata"] = report_profile_plan.change.after_metadata
            if existing_is_company:
                from src.dalton.services.company_agent_writes import update_company_agent

                try:
                    company_row, route_warning = await execute_report_profile_rest_mutation(
                        change=report_profile_plan.change,
                        actor_user_id=user_id,
                        expected_config_version=eff_expected_cv,
                        is_company=True,
                        confirmation_token=report_profile_plan.confirmation_token,
                        confirmation_action=report_profile_plan.confirmation_action,
                        confirmation_automation_id=report_profile_plan.confirmation_automation_id,
                        confirmation_config_version=report_profile_plan.confirmation_config_version,
                        mutate_fn=lambda db_conn: update_company_agent(
                            principal,
                            agent_id,
                            fields,
                            request=request,
                            expected_config_version=eff_expected_cv,
                            conn=db_conn,
                        ),
                    )
                except ReportDeliveryError as exc:
                    raise _report_delivery_http_error(exc) from None
                if not company_row:
                    raise_not_found("Automation")
                company_resp = _company_write_response(company_row)
                from src.dalton.services.report_delivery import build_missing_report_profile_warning

                company_resp.report_delivery_warning = build_missing_report_profile_warning(
                    company_resp.instruction, company_resp.metadata
                )
                company_resp.route_warning = route_warning
                return company_resp
            try:
                automation, route_warning = await execute_report_profile_rest_mutation(
                    change=report_profile_plan.change,
                    actor_user_id=user_id,
                    expected_config_version=eff_expected_cv,
                    confirmation_token=report_profile_plan.confirmation_token,
                    confirmation_action=report_profile_plan.confirmation_action,
                    confirmation_automation_id=report_profile_plan.confirmation_automation_id,
                    confirmation_config_version=report_profile_plan.confirmation_config_version,
                    mutate_fn=lambda db_conn: handler.update_automation(
                        automation_id=agent_id,
                        user_id=user_id,
                        fields=fields,
                        conn=db_conn,
                    ),
                )
            except ReportDeliveryError as exc:
                raise _report_delivery_http_error(exc) from None
        else:
            with automation_history_context(
                actor_user_id=user_id,
                action="update",
                expected_config_version=expected_cv,
            ):
                automation = await handler.update_automation(
                    automation_id=agent_id,
                    user_id=user_id,
                    fields=fields,
                )
    except handler.DeliveryRefused as e:
        return _delivery_refused(e)
    except handler.AutomationRefusal as e:
        # DALTON-HOOK(automation-effort): map invalid reasoning_effort refusals to HTTP 422
        if e.field == "reasoning_effort":
            raise HTTPException(status_code=422, detail=str(e)) from None
        raise
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
        # DALTON-HOOK(report-delivery): warn when an updated report-skill automation has no valid report_profile or unsynced route
        if dalton_enabled:
            from src.dalton.services.report_delivery import build_missing_report_profile_warning

            resp.report_delivery_warning = build_missing_report_profile_warning(
                resp.instruction, resp.metadata
            )
            resp.route_warning = route_warning
        return resp
    # DALTON-HOOK(company-automations): a miss on the caller may be an existing company row
    from src.dalton.services.company_agent_writes import update_company_agent

    company_row = await update_company_agent(
        principal,
        agent_id,
        fields,
        request=request,
        expected_config_version=expected_cv,
    )
    if not company_row:
        raise_not_found("Automation")
    company_resp = _company_write_response(company_row)
    # DALTON-HOOK(report-delivery): warn when an updated company report-skill automation has no valid report_profile
    if dalton_enabled:
        from src.dalton.services.report_delivery import build_missing_report_profile_warning

        company_resp.report_delivery_warning = build_missing_report_profile_warning(
            company_resp.instruction, company_resp.metadata
        )
    return company_resp


def _positive_recycle_version(value: Any, field_name: str) -> int:
    """400 for a missing or non-positive version. Not a ValueError, so it stays 400."""
    if field_name == "expected_config_version":
        # provided=True never returns None; a bad value is already HTTP 400.
        return validate_expected_config_version(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be a positive integer",
        )
    return value


# DALTON-HOOK(company-automations): soft delete, restore, and one-step rollback
@router.delete("/automations/{automation_id}", response_model=AutomationReadResponse)
@handle_api_exceptions("delete automation", logger, conflict_on_value_error=True)
async def delete_automation(
    automation_id: str,
    principal: WorkspaceDeletePrincipal,
    body: RecycleVersionRequest,
    http_request: Request,
):
    """Soft-delete an automation or a ``data:<job_key>`` into the recycle bin."""
    version = _positive_recycle_version(
        body.expected_config_version,
        "expected_config_version",
    )
    if uuid_or_none(automation_id) is not None:
        await cli_approval.automation_delete(http_request, principal, automation_id)
    from src.dalton.services.automation_recycle import delete_task

    return await delete_task(principal, automation_id, version)


@router.post("/automations/{automation_id}/restore", response_model=AutomationReadResponse)
@handle_api_exceptions("restore automation", logger, conflict_on_value_error=True)
async def restore_automation(
    automation_id: str,
    principal: WorkspaceWritePrincipal,
    body: RecycleVersionRequest,
):
    """Restore a soft-deleted automation or data task."""
    version = _positive_recycle_version(
        body.expected_config_version,
        "expected_config_version",
    )
    from src.dalton.services.automation_recycle import restore_task

    return await restore_task(principal, automation_id, version)


@router.post("/automations/{automation_id}/rollback", response_model=AutomationReadResponse)
@handle_api_exceptions("rollback automation", logger, conflict_on_value_error=True)
async def rollback_automation(
    automation_id: str,
    principal: WorkspaceWritePrincipal,
    body: AutomationRollbackRequest,
):
    """Roll an automation or data task back one configuration version."""
    version = _positive_recycle_version(
        body.expected_config_version,
        "expected_config_version",
    )
    target = _positive_recycle_version(body.target_version, "target_version")
    from src.dalton.services.automation_recycle import rollback_task

    return await rollback_task(
        principal,
        automation_id,
        expected_config_version=version,
        target_version=target,
    )


# =============================================================================
# Control Endpoints
# =============================================================================


@router.post("/automations/{automation_id}/trigger")
@handle_api_exceptions("trigger automation", logger, conflict_on_value_error=True)
async def trigger_automation(
    automation_id: UUID,
    principal: RunPrincipal,
    http_request: Request,
):
    """Manually trigger an automation immediately (doesn't affect next_run_at)."""
    checked = await cli_approval.automation_runs(
        http_request, principal, "trigger", automation_id=str(automation_id)
    )
    result = await handler.trigger_automation(
        str(automation_id), principal.user_id, checked_computer_id=checked
    )
    return result


# DALTON-HOOK(company-automations): data pause requires a version; agent pause stays optional
@router.post("/automations/{automation_id}/pause", response_model=AutomationReadResponse)
@handle_api_exceptions("pause automation", logger, conflict_on_value_error=True)
async def pause_automation(
    automation_id: str,
    principal: WorkspaceWritePrincipal,
    body: Any = Body(default=None),
):
    """Pause an active automation."""
    # DALTON-HOOK(company-automations): classify the id before reading the pause body
    parsed = _parse_read_id(automation_id)
    if parsed.kind == "script":
        version = _script_expected_version(body)
        from src.dalton.services.company_script_writes import pause_company_script

        return await pause_company_script(
            principal,
            str(parsed.job_key),
            expected_config_version=version,
        )
    agent_id = str(parsed.agent_id)
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
        automation = await handler.pause_automation(agent_id, user_id)
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
        agent_id,
        expected_config_version=expected_cv,
    )
    if not company_row:
        raise_not_found("Automation")
    return _company_write_response(company_row)


# DALTON-HOOK(company-automations): data resume requires a version; agent resume stays optional
@router.post("/automations/{automation_id}/resume", response_model=AutomationReadResponse)
@handle_api_exceptions("resume automation", logger, conflict_on_value_error=True)
async def resume_automation(
    automation_id: str,
    # Re-arms runs into the automation's thread, so not the call's own.
    principal: WorkspaceSendPrincipal,
    http_request: Request,
    body: Any = Body(default=None),
):
    """Resume a paused or disabled automation."""
    # DALTON-HOOK(company-automations): classify the id before reading the resume body
    parsed = _parse_read_id(automation_id)
    if parsed.kind == "script":
        version = _script_expected_version(body)
        from src.dalton.services.company_script_writes import resume_company_script

        return await resume_company_script(
            principal,
            str(parsed.job_key),
            expected_config_version=version,
        )
    agent_id = str(parsed.agent_id)
    user_id = principal.user_id
    # DALTON-HOOK(company-automations): company system identity cannot resume through this route
    _refuse_company_service_actor(user_id)
    # Its runs start again where it runs, so off the token's computer it asks.
    await cli_approval.automation_runs(
        http_request, principal, "resume", automation_id=agent_id
    )
    # DALTON-HOOK(company-automations): validate optional expected_config_version on resume
    expected_cv = extract_expected_config_version(body)
    with automation_history_context(
        actor_user_id=user_id,
        action="resume",
        expected_config_version=expected_cv,
    ):
        automation = await handler.resume_automation(agent_id, user_id)
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
        agent_id,
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
    items = [AutomationRunResponse.model_validate(e) for e in executions]
    # DALTON-HOOK(execution-delivery-status): enrich run feed and per-automation history with delivery_summary
    from src.dalton.services.execution_delivery_status import (
        enrich_executions_with_delivery_summary,
    )

    await enrich_executions_with_delivery_summary(items)
    return AutomationRunsListResponse(
        executions=items,
        has_more=has_more,
    )
