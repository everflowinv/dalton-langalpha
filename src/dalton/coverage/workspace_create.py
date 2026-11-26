"""Dalton web workspace-creation orchestration (dedup, locked recheck retry, 409s, same-tx company identity).

Lives under ``src/dalton/coverage/`` so upstream ``src/server/app/workspaces.py``
retains only a thin ``DALTON-HOOK(workspace-dedup)`` call.
"""

from __future__ import annotations

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


def _build_in_tx_identity_hook(
    *,
    user_id: str,
    request: Any,
    principal: Any,
    wants_coverage: bool,
    raw_coverage_kind: str,
    explicit_ticker_key: str | None,
    registration_state: dict[str, Any],
) -> Any | None:
    """Build an ``on_workspace_inserted(conn, ws_row)`` hook that commits
    company identity (coverage unit and/or canonical company ticker config) in
    the exact same transaction as ``INSERT INTO workspaces``."""
    should_stamp_ticker = bool(
        explicit_ticker_key is not None and raw_coverage_kind != "sector"
    )
    if not wants_coverage and not should_stamp_ticker:
        return None

    async def _on_workspace_inserted(conn: Any, ws_row: dict[str, Any]) -> None:
        ws_id = str(ws_row["workspace_id"])
        ws_name = str(ws_row.get("name") or request.name)
        registered_company_unit = False

        if wants_coverage:
            try:
                async with conn.transaction():
                    result = await units_mod.auto_register_workspace_coverage_unit(
                        user_id,
                        workspace_id=ws_id,
                        workspace_name=ws_name,
                        coverage_kind=request.coverage_kind,
                        ticker=request.ticker,
                        display_name=request.display_name,
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
        # whenever an explicit company ticker was supplied and no bound company
        # coverage unit carries it yet (e.g. ticker without coverage_kind,
        # external caller, or non-fatal unit warning).
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
                                Json({WORKSPACE_COMPANY_TICKER_CONFIG_KEY: explicit_ticker_key}),
                                ws_id,
                            ),
                        )
                cfg = dict(ws_row.get("config") or {})
                cfg[WORKSPACE_COMPANY_TICKER_CONFIG_KEY] = explicit_ticker_key
                ws_row["config"] = cfg
            except Exception as exc:
                logger.warning(
                    "In-transaction company ticker stamp failed for workspace %s (%s)",
                    ws_id,
                    type(exc).__name__,
                )

    return _on_workspace_inserted


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
    if registration_state["ran"]:
        unit_row, unit_warning, unit_warning_detail = registration_state["result"]
    else:
        # Fallback when WorkspaceManager.create_workspace was replaced by a
        # unit-test mock that did not invoke create_workspace_on_computer.
        unit_row, unit_warning, unit_warning_detail = (
            await units_mod.auto_register_workspace_coverage_unit(
                user_id,
                workspace_id=str(workspace["workspace_id"]),
                workspace_name=str(workspace.get("name") or request.name),
                coverage_kind=request.coverage_kind,
                ticker=request.ticker,
                display_name=request.display_name,
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

    explicit_ticker_raw = str(request.ticker or "").strip() or None
    explicit_ticker_key = (
        normalize_ticker_key(explicit_ticker_raw)
        if explicit_ticker_raw and raw_coverage_kind != "sector"
        else None
    )

    if request.allow_duplicate_company:
        matched_ids: list[str] = []
        try:
            dup_for_log = await dedup_mod.find_existing_company_workspace(
                x_user_id, request.name, channel="web"
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
        in_tx_hook = _build_in_tx_identity_hook(
            user_id=x_user_id,
            request=request,
            principal=principal,
            wants_coverage=wants_coverage,
            raw_coverage_kind=raw_coverage_kind,
            explicit_ticker_key=explicit_ticker_key,
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

    for attempt in range(MAX_WEB_DEDUP_ATTEMPTS):
        dup = None
        dedup_snapshots: list[Any] = []
        phase1_failed = False
        try:
            dup = await dedup_mod.find_existing_company_workspace(
                x_user_id,
                request.name,
                channel="web",
                snapshot_out=dedup_snapshots,
            )
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

        registration_state = {"ran": False, "result": (None, None, None)}
        in_tx_hook = _build_in_tx_identity_hook(
            user_id=x_user_id,
            request=request,
            principal=principal,
            wants_coverage=wants_coverage,
            raw_coverage_kind=raw_coverage_kind,
            explicit_ticker_key=explicit_ticker_key,
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

        effective_ticker = explicit_ticker_raw or (
            dup.suggestion.ticker
            if dup is not None and dup.suggestion is not None
            else None
        )

        dedup_snap = dedup_snapshots[-1] if dedup_snapshots else None
        if dedup_snap is not None and not dedup_snap.active:
            # Dedup explicitly disabled (e.g. single-user OSS / coverage units off / company user).
            pass
        elif dedup_snap is not None and dedup_snap.active and dedup_snap.status == "clear":
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
    return await _attach_coverage_registration_result(
        workspace,
        user_id=x_user_id,
        request=request,
        principal=principal,
        wants_coverage=wants_coverage,
        registration_state=registration_state,
    )
