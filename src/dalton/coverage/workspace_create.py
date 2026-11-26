"""Dalton web workspace-creation orchestration (dedup, locked recheck retry, 409s, same-tx company identity).

Lives under ``src/dalton/coverage/`` so upstream ``src/server/app/workspaces.py``
retains only a thin ``DALTON-HOOK(workspace-dedup)`` call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from fastapi import HTTPException
from psycopg.types.json import Json

from src.dalton.coverage import dedup as dedup_mod
from src.dalton.coverage.identity import normalize_ticker_key
from src.dalton.coverage.locked_recheck import WORKSPACE_COMPANY_TICKER_CONFIG_KEY
from src.dalton.coverage import units as units_mod
from src.server.services.computer_errors import DuplicateWorkspaceConflictError

logger = logging.getLogger(__name__)

MAX_WEB_DEDUP_ATTEMPTS = 3


def _raise_company_workspace_exists(dup_match: Any) -> None:
    detail: dict[str, Any] = {
        "code": "company_workspace_exists",
        "message": dup_match.message,
        "matches": dup_match.matches_payload(),
    }
    suggestion = dup_match.suggestion_payload()
    if suggestion is not None:
        detail["suggestion"] = suggestion
    raise HTTPException(
        status_code=409,
        detail=detail,
    )


def _strip_internal_config_keys(config: dict[str, Any] | None) -> dict[str, Any] | None:
    if config is None:
        return None
    if WORKSPACE_COMPANY_TICKER_CONFIG_KEY not in config:
        return config
    return {
        k: v
        for k, v in config.items()
        if k != WORKSPACE_COMPANY_TICKER_CONFIG_KEY
    }


async def _resolve_fallback_colleague_check(user_id: str) -> bool:
    """Safely determine colleague scope when Phase 1 produced no snapshot."""
    if not dedup_mod.is_colleague_dedup_enabled():
        return False
    try:
        caller_tier = await dedup_mod._resolve_caller_tier(user_id)
        return caller_tier not in ("external", "company")
    except Exception:
        # Never risk leaking colleague existence to an external member when
        # tier lookup itself failed; still enforce caller's own locked recheck.
        return False


def resolve_effective_company_ticker(
    *,
    raw_coverage_kind: str,
    explicit_ticker: str | None,
    suggestion: Any | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(effective_ticker_raw, effective_ticker_key)`` for company dedup
    locking and same-transaction identity persistence.

    Uses the exact same effective company ticker for both Phase 2 ``ws_ticker:``
    locking / recheck and in-transaction ``dalton_company_ticker`` stamping
    (including web "create without registering" where only ``suggestion.ticker``
    is present). Never returns a ticker for sector workspaces.
    """
    if raw_coverage_kind == "sector":
        return None, None
    explicit_raw = str(explicit_ticker or "").strip() or None
    suggested_raw = (
        str(getattr(suggestion, "ticker", None) or "").strip() or None
        if (
            suggestion is not None
            and getattr(suggestion, "coverage_kind", None) == "company"
        )
        else None
    )
    effective_raw = explicit_raw or suggested_raw
    effective_key = normalize_ticker_key(effective_raw) if effective_raw else None
    return effective_raw, effective_key


async def resolve_cos_colleague_dedup(user_id: str) -> bool:
    """Return True when Chief of Staff locked recheck should include colleague
    workspaces and acquire the shared normalized ``ws_ticker:`` advisory lock."""
    if not dedup_mod.is_company_dedup_active():
        return False
    from src.dalton.company_identity import is_company_system_user

    if is_company_system_user(user_id):
        return False
    return await _resolve_fallback_colleague_check(user_id)


async def format_cos_duplicate_conflict_error(
    user_id: str,
    name: str,
    conflict: DuplicateWorkspaceConflictError,
) -> tuple[str, str, dict[str, Any]]:
    """Convert a locked-recheck ``DuplicateWorkspaceConflictError`` into a
    Chief of Staff tool error tuple ``(message, code, extra_fields)``."""
    if conflict.conflicting_workspace_id:
        det_dup = await dedup_mod.build_deterministic_duplicate_match(
            user_id,
            name,
            conflict.conflicting_workspace_id,
            channel="cos",
        )
        if det_dup is not None and det_dup.matches:
            extra_fields: dict[str, Any] = {
                "message": det_dup.message,
                "matches": det_dup.matches_payload(),
            }
            suggestion = det_dup.suggestion_payload()
            if suggestion is not None:
                extra_fields["suggestion"] = suggestion
            return det_dup.message, "company_workspace_exists", extra_fields
    return "情况已变化，请重新发起", "scope_changed", {"message_status": "error"}


def build_in_tx_identity_hook(
    *,
    user_id: str,
    workspace_name: str,
    coverage_kind: str | None,
    ticker: str | None,
    display_name: str | None,
    wants_coverage: bool,
    raw_coverage_kind: str,
    effective_ticker_key: str | None,
    registration_state: dict[str, Any],
    principal: Any = None,
) -> Any | None:
    """Build an ``on_workspace_inserted(conn, ws_row)`` hook that commits
    company identity (coverage unit and/or canonical company ticker config) in
    the exact same transaction as ``INSERT INTO workspaces``."""
    should_stamp_ticker = bool(
        effective_ticker_key is not None and raw_coverage_kind != "sector"
    )
    if not wants_coverage and not should_stamp_ticker:
        return None

    async def _on_workspace_inserted(conn: Any, ws_row: dict[str, Any]) -> None:
        ws_id = str(ws_row["workspace_id"])
        ws_name = str(ws_row.get("name") or workspace_name)
        registered_company_unit = False

        if wants_coverage:
            try:
                async with conn.transaction():
                    result = await units_mod.auto_register_workspace_coverage_unit(
                        user_id,
                        workspace_id=ws_id,
                        workspace_name=ws_name,
                        coverage_kind=coverage_kind,
                        ticker=ticker,
                        display_name=display_name,
                        principal=principal,
                        conn=conn,
                    )
                registration_state["ran"] = True
                registration_state["result"] = result
                unit_row = result[0]
                if (
                    isinstance(unit_row, dict)
                    and str(unit_row.get("kind") or "").lower() == "company"
                    and unit_row.get("ticker")
                ):
                    registered_company_unit = True
            except Exception as exc:
                logger.warning(
                    "In-transaction coverage unit registration failed for workspace %s (%s)",
                    ws_id,
                    type(exc).__name__,
                )
                registration_state["ran"] = True
                msg, detail = units_mod._build_auto_register_warning("registration_failed")
                registration_state["result"] = (None, msg, detail)

        # Stamp canonical company ticker onto workspace.config in the same tx
        # whenever an effective company ticker was resolved and no bound company
        # coverage unit carries it yet (e.g. ticker without coverage_kind, web
        # "create without registering" with model-suggested ticker, external
        # caller, or non-fatal unit warning).
        if should_stamp_ticker and not registered_company_unit:
            try:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            UPDATE workspaces
                            SET config = COALESCE(config, '{}'::jsonb) || %s::jsonb
                            WHERE workspace_id = %s::uuid
                            """,
                            (
                                Json({WORKSPACE_COMPANY_TICKER_CONFIG_KEY: effective_ticker_key}),
                                ws_id,
                            ),
                        )
                cfg = dict(ws_row.get("config") or {})
                cfg[WORKSPACE_COMPANY_TICKER_CONFIG_KEY] = effective_ticker_key
                ws_row["config"] = cfg
            except Exception as exc:
                logger.warning(
                    "In-transaction company ticker stamp failed for workspace %s (%s)",
                    ws_id,
                    type(exc).__name__,
                )

    return _on_workspace_inserted


async def resolve_coverage_registration_result(
    *,
    user_id: str,
    workspace_id: str,
    workspace_name: str,
    coverage_kind: str | None,
    ticker: str | None,
    display_name: str | None,
    wants_coverage: bool,
    registration_state: dict[str, Any],
    principal: Any = None,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
    if not wants_coverage:
        return None, None, None
    if registration_state["ran"]:
        return registration_state["result"]
    # Fallback when WorkspaceManager.create_workspace was replaced by a
    # unit-test mock that did not invoke create_workspace_on_computer.
    return await units_mod.auto_register_workspace_coverage_unit(
        user_id,
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        coverage_kind=coverage_kind,
        ticker=ticker,
        display_name=display_name,
        principal=principal,
    )


async def _attach_coverage_registration_result(
    workspace: dict[str, Any],
    *,
    user_id: str,
    request: Any,
    principal: Any,
    wants_coverage: bool,
    registration_state: dict[str, Any],
) -> dict[str, Any]:
    if not wants_coverage:
        return workspace
    unit_row, unit_warning, unit_warning_detail = (
        await resolve_coverage_registration_result(
            user_id=user_id,
            workspace_id=str(workspace["workspace_id"]),
            workspace_name=str(workspace.get("name") or request.name),
            coverage_kind=request.coverage_kind,
            ticker=request.ticker,
            display_name=request.display_name,
            wants_coverage=wants_coverage,
            registration_state=registration_state,
            principal=principal,
        )
    )
    if (
        unit_row is not None
        or unit_warning is not None
        or unit_warning_detail is not None
    ):
        workspace = dict(workspace)
        if unit_row is not None:
            workspace["coverage_unit"] = unit_row
        if unit_warning is not None:
            workspace["coverage_unit_warning"] = unit_warning
        if unit_warning_detail is not None:
            workspace["coverage_unit_warning_detail"] = unit_warning_detail
    return workspace


async def create_workspace_for_web(
    *,
    request: Any,
    principal: Any,
    computer_id: str | None,
    get_manager: Callable[[], Any],
    cause_factory: Callable[[str], Any],
) -> dict[str, Any]:
    """Run web workspace creation with Dalton company dedup, locked recheck
    retry, 409 responses, and same-transaction company identity commit."""
    x_user_id = principal.user_id
    sanitized_config = _strip_internal_config_keys(request.config)

    raw_coverage_kind = str(request.coverage_kind or "").strip().lower()
    requested_coverage = raw_coverage_kind not in ("", "none", "other")
    coverage_active: bool | None = None
    if requested_coverage:
        coverage_active = await units_mod.is_workspace_coverage_registration_active(x_user_id)
    wants_coverage = bool(requested_coverage and coverage_active)

    _, explicit_ticker_key = resolve_effective_company_ticker(
        raw_coverage_kind=raw_coverage_kind,
        explicit_ticker=request.ticker,
    )

    if request.allow_duplicate_company:
        matched_ids: list[str] = []
        try:
            dup_for_log = await asyncio.wait_for(
                dedup_mod.find_existing_company_workspace(
                    x_user_id, request.name, channel="web"
                ),
                timeout=dedup_mod.WEB_DEDUP_OVERALL_TIMEOUT_SECONDS,
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
            x_user_id,
            request.name,
            matched_ids,
        )
        registration_state: dict[str, Any] = {"ran": False, "result": (None, None, None)}
        in_tx_hook = build_in_tx_identity_hook(
            user_id=x_user_id,
            workspace_name=request.name,
            coverage_kind=request.coverage_kind,
            ticker=request.ticker,
            display_name=request.display_name,
            principal=principal,
            wants_coverage=wants_coverage,
            raw_coverage_kind=raw_coverage_kind,
            effective_ticker_key=explicit_ticker_key,
            registration_state=registration_state,
        )
        create_kwargs: dict[str, Any] = {
            "user_id": x_user_id,
            "name": request.name,
            "description": request.description,
            "config": sanitized_config,
            "computer_id": computer_id,
        }
        if in_tx_hook is not None:
            create_kwargs["on_workspace_inserted"] = in_tx_hook
        manager = get_manager()
        with cause_factory("create"):
            workspace = await manager.create_workspace(**create_kwargs)
        return await _attach_coverage_registration_result(
            workspace,
            user_id=x_user_id,
            request=request,
            principal=principal,
            wants_coverage=wants_coverage,
            registration_state=registration_state,
        )

    workspace: dict[str, Any] | None = None
    registration_state = {"ran": False, "result": (None, None, None)}
    dedup_warning: str | None = None
    dedup_warning_code: str | None = None

    for attempt in range(MAX_WEB_DEDUP_ATTEMPTS):
        dup = None
        dedup_snapshots: list[Any] = []
        phase1_failed = False
        dedup_warning = None
        dedup_warning_code = None
        try:
            dup = await asyncio.wait_for(
                dedup_mod.find_existing_company_workspace(
                    x_user_id,
                    request.name,
                    channel="web",
                    raise_on_timeout=True,
                    snapshot_out=dedup_snapshots,
                ),
                timeout=dedup_mod.WEB_DEDUP_OVERALL_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError):
            phase1_failed = True
            logger.warning(
                "Company workspace dedup check timed out after %.1fs; falling back to locked recheck",
                dedup_mod.WEB_DEDUP_OVERALL_TIMEOUT_SECONDS,
            )
            dedup_warning = dedup_mod.WEB_DEDUP_UNAVAILABLE_MESSAGE
            dedup_warning_code = dedup_mod.WEB_DEDUP_UNAVAILABLE_CODE
        except Exception as exc:
            phase1_failed = True
            logger.warning(
                "Company workspace dedup check failed (%s); falling back to locked recheck",
                type(exc).__name__,
            )

        if dup is not None and dup.matches:
            _raise_company_workspace_exists(dup)
        if (
            dup is not None
            and dup.suggestion is not None
            and not wants_coverage
            and not request.skip_coverage_suggestion
            and principal.unlimited
        ):
            if coverage_active is None:
                coverage_active = await units_mod.is_workspace_coverage_registration_active(
                    x_user_id
                )
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
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "coverage_suggestion",
                        "message": f"「{request.name}」识别为{kind_label}{ticker_note}，是否自动登记覆盖单元并创建工作区？",
                        "suggestion": suggestion,
                    },
                )

        effective_ticker, effective_ticker_key = resolve_effective_company_ticker(
            raw_coverage_kind=raw_coverage_kind,
            explicit_ticker=request.ticker,
            suggestion=dup.suggestion if dup is not None else None,
        )
        registration_state = {"ran": False, "result": (None, None, None)}
        in_tx_hook = build_in_tx_identity_hook(
            user_id=x_user_id,
            workspace_name=request.name,
            coverage_kind=request.coverage_kind,
            ticker=request.ticker,
            display_name=request.display_name,
            principal=principal,
            wants_coverage=wants_coverage,
            raw_coverage_kind=raw_coverage_kind,
            effective_ticker_key=effective_ticker_key,
            registration_state=registration_state,
        )

        create_kwargs = {
            "user_id": x_user_id,
            "name": request.name,
            "description": request.description,
            "config": sanitized_config,
            "computer_id": computer_id,
        }
        if in_tx_hook is not None:
            create_kwargs["on_workspace_inserted"] = in_tx_hook

        dedup_snap = dedup_snapshots[-1] if dedup_snapshots else None
        if not phase1_failed and dedup_snap is not None and not dedup_snap.active:
            # Dedup explicitly disabled (e.g. single-user OSS / coverage units off / company user).
            pass
        elif (
            not phase1_failed
            and dedup_snap is not None
            and dedup_snap.active
            and dedup_snap.status == "clear"
        ):
            create_kwargs["check_duplicate_snapshot"] = True
            create_kwargs["verified_company_candidates"] = dedup_snap.checked_candidates
            if dedup_snap.check_colleague_workspaces:
                create_kwargs["check_colleague_duplicate"] = True
            if effective_ticker:
                create_kwargs["target_ticker"] = effective_ticker
        else:
            # Phase 1 failed to collect a snapshot (or snapshot is unavailable).
            # Explicitly run locked deterministic recheck instead of creating unchecked.
            if dedup_mod.is_company_dedup_active():
                from src.dalton.company_identity import is_company_system_user

                if not is_company_system_user(x_user_id):
                    if not phase1_failed:
                        logger.warning(
                            "Company workspace dedup snapshot unavailable; enforcing locked deterministic recheck"
                        )
                    create_kwargs["check_duplicate_snapshot"] = True
                    if dedup_snap is not None and dedup_snap.check_colleague_workspaces:
                        create_kwargs["check_colleague_duplicate"] = True
                    elif dedup_snap is None and await _resolve_fallback_colleague_check(
                        x_user_id
                    ):
                        create_kwargs["check_colleague_duplicate"] = True
                    if effective_ticker:
                        create_kwargs["target_ticker"] = effective_ticker

        manager = get_manager()
        try:
            with cause_factory("create"):
                workspace = await manager.create_workspace(**create_kwargs)
            break
        except DuplicateWorkspaceConflictError as conflict:
            # Only synthesize company_workspace_exists when locked recheck
            # identified a concrete conflicting workspace ID. Drift without an
            # ID must never be reported as "already exists".
            if conflict.conflicting_workspace_id:
                dup_after = None
                try:
                    dup_after = await dedup_mod.find_existing_company_workspace(
                        x_user_id, request.name, channel="web"
                    )
                except Exception as exc:
                    logger.warning(
                        "Post-conflict company dedup check failed (%s)",
                        type(exc).__name__,
                    )
                if dup_after is not None and dup_after.matches:
                    _raise_company_workspace_exists(dup_after)
                det_dup = await dedup_mod.build_deterministic_duplicate_match(
                    x_user_id,
                    request.name,
                    conflict.conflicting_workspace_id,
                    channel="web",
                )
                if det_dup is not None and det_dup.matches:
                    _raise_company_workspace_exists(det_dup)
            if attempt + 1 < MAX_WEB_DEDUP_ATTEMPTS:
                continue
            dup_final = None
            try:
                dup_final = await dedup_mod.find_existing_company_workspace(
                    x_user_id, request.name, channel="web"
                )
            except Exception:
                dup_final = None
            if dup_final is not None and dup_final.matches:
                _raise_company_workspace_exists(dup_final)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "workspace_create_conflict",
                    "message": "工作区列表刚刚发生变化，请重试。",
                },
            ) from None

    assert workspace is not None
    workspace = await _attach_coverage_registration_result(
        workspace,
        user_id=x_user_id,
        request=request,
        principal=principal,
        wants_coverage=wants_coverage,
        registration_state=registration_state,
    )
    if dedup_warning is not None:
        workspace = dict(workspace)
        workspace["dedup_warning"] = dedup_warning
        workspace["dedup_warning_code"] = dedup_warning_code
    return workspace
