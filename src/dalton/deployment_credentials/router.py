"""Administrator REST router for deployment-level integration credentials (§3.1, §3.2).

Mounts ``GET /api/v1/dalton/deployment-settings`` and
``PUT /api/v1/dalton/deployment-settings`` behind ``AdminSessionUserId`` and a
custom ``APIRoute`` class that strips raw request inputs from all 422 / 4xx / 5xx
responses so submitted secrets never echo back in error bodies or logs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine, Mapping
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from src.dalton.auth.sessions import extract_client_ip
from src.dalton.deployment_credentials.auth import AdminSessionUserId
from src.dalton.deployment_credentials.catalog import (
    DaltonDeploymentFieldSpec,
    get_field_specs,
    group_keys,
)
from src.dalton.deployment_credentials.probes import ProbeTarget, run_credential_probe
from src.dalton.deployment_credentials.redirect_uri import google_redirect_uri_eligible
from src.dalton.deployment_credentials.snapshot import (
    CredentialSource,
    preload_feishu_vault_pair,
)
from src.dalton.deployment_credentials.store import (
    DeploymentSettingConflictError,
    DeploymentSettingEncryptionUnavailableError,
    DeploymentSettingValidationError,
    read_settings_snapshot_detailed,
    write_settings,
)
from src.dalton.integrations.constants import (
    build_feishu_integration_events_url,
    build_integration_redirect_uri,
)
from src.dalton.profile import resolve_profile
from src.dalton.settings import get_dalton_public_app_url, is_dalton_enabled
from src.server.database.encryption import encryption_configured
from src.server.services.setup_settings import environment as setup_env

logger = logging.getLogger(__name__)

_NO_STORE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}


def _require_dalton_enabled() -> None:
    if not is_dalton_enabled():
        raise HTTPException(status_code=404, detail={"code": "dalton_disabled"})


_REQUEST_MODEL_FIELDS: frozenset[str] = frozenset({"expected_revision", "values", "target"})


def _sanitize_request_validation_error(exc: RequestValidationError) -> list[dict[str, str]]:
    """Convert Pydantic/FastAPI validation errors to enum reasons with allowlisted field names only."""
    allowed_names = (
        {spec.name for spec in get_field_specs(profile=resolve_profile())}
        | _REQUEST_MODEL_FIELDS
    )
    sanitized: list[dict[str, str]] = []
    for err in exc.errors():
        loc = err.get("loc") or ()
        err_type = str(err.get("type") or "")
        if not loc or loc == ("body",):
            setting_name = "_body"
        else:
            last = loc[-1]
            if isinstance(last, int) and not isinstance(last, bool):
                setting_name = "_body"
            else:
                if len(loc) >= 3 and str(loc[1]) == "values":
                    candidate = str(loc[2])
                else:
                    candidate = str(last)
                setting_name = candidate if candidate in allowed_names else "_unknown"

        if err_type == "extra_forbidden":
            reason = "unknown_setting"
        elif "too_long" in err_type or "max_length" in err_type:
            reason = "value_too_long"
        else:
            reason = "non_printable"
        sanitized.append({"setting_name": setting_name, "reason": reason})

    if not sanitized:
        sanitized.append({"setting_name": "_body", "reason": "non_printable"})
    return sanitized


class RedactedDeploymentSettingsRoute(APIRoute):
    """Route handler wrapper guaranteeing redacted JSON error payloads on all failures."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_route_handler = super().get_route_handler()

        async def _handled(request: Request) -> Response:
            try:
                return await original_route_handler(request)
            except RequestValidationError as exc:
                return JSONResponse(
                    status_code=422,
                    content={
                        "code": "validation_error",
                        "errors": _sanitize_request_validation_error(exc),
                    },
                    headers=_NO_STORE_HEADERS,
                )
            except DeploymentSettingValidationError as exc:
                return JSONResponse(
                    status_code=422,
                    content={
                        "code": "validation_error",
                        "errors": exc.errors,
                    },
                    headers=_NO_STORE_HEADERS,
                )
            except DeploymentSettingConflictError as exc:
                payload: dict[str, Any] = {"code": exc.code}
                if exc.setting_name is not None:
                    payload["setting_name"] = exc.setting_name
                if exc.current_revision is not None:
                    payload["current_revision"] = exc.current_revision
                return JSONResponse(
                    status_code=409,
                    content=payload,
                    headers=_NO_STORE_HEADERS,
                )
            except DeploymentSettingEncryptionUnavailableError as exc:
                return JSONResponse(
                    status_code=503,
                    content={"code": exc.code},
                    headers=_NO_STORE_HEADERS,
                )
            except HTTPException:
                raise
            except Exception as exc:
                logger.error(
                    "Deployment settings request failed (%s)",
                    type(exc).__name__,
                )
                return JSONResponse(
                    status_code=500,
                    content={"code": "internal_error"},
                    headers=_NO_STORE_HEADERS,
                )

        return _handled


class DeploymentSettingsUpdateRequest(BaseModel):
    """Payload for ``PUT /api/v1/dalton/deployment-settings``."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int | None = None
    values: dict[str, Any] = Field(default_factory=dict)


class DeploymentSettingsTestRequest(BaseModel):
    """Payload for ``POST /api/v1/dalton/deployment-settings/test``."""

    model_config = ConfigDict(extra="forbid")

    target: ProbeTarget
    values: dict[str, Any] = Field(default_factory=dict)


router = APIRouter(
    prefix="/api/v1/dalton/deployment-settings",
    tags=["Dalton Deployment Credentials"],
    dependencies=[Depends(_require_dalton_enabled)],
    route_class=RedactedDeploymentSettingsRoute,
)


def _resolve_field_view(
    spec: DaltonDeploymentFieldSpec,
    *,
    saved_rows: Mapping[str, str],
    updated_at_map: Mapping[str, datetime],
    feishu_vault_pair: Mapping[str, str] | None,
    feishu_vault_error: bool,
) -> dict[str, Any]:
    """Build the redacted API view for one deployment setting field."""
    at_start = setup_env._snapshot()

    source: CredentialSource
    if spec.credential_group:
        members = group_keys(spec.credential_group)
        if any(setup_env.set_outside_app(k) for k in members):
            source = "env"
        elif any(k in saved_rows for k in members):
            source = "saved"
        elif spec.credential_group == "feishu_app":
            if feishu_vault_error:
                source = "vault_error"
            elif feishu_vault_pair is not None:
                source = "vault_readonly"
            else:
                source = "unset"
        else:
            source = "unset"
    else:
        if setup_env.set_outside_app(spec.name):
            source = "env"
        elif spec.name in saved_rows:
            source = "saved"
        else:
            source = "unset"

    readonly = source in ("env", "vault_readonly", "vault_error")

    effective_val: str | None = None
    if source == "env":
        raw = at_start.get(spec.name)
        effective_val = raw if raw else None
    elif source == "saved":
        effective_val = saved_rows.get(spec.name)
    elif source == "vault_readonly":
        effective_val = (feishu_vault_pair or {}).get(spec.name)

    if spec.input_kind in ("boolean", "boolean_one") and source == "saved":
        configured = spec.name in saved_rows
    elif source == "vault_error":
        configured = False
    else:
        configured = bool(effective_val)

    ts = updated_at_map.get(spec.name) if source == "saved" else None
    updated_at = ts.isoformat() if isinstance(ts, datetime) else None

    entry: dict[str, Any] = {
        "name": spec.name,
        "section": spec.section,
        "label_zh": spec.label_zh,
        "label_en": spec.label_en,
        "secret": spec.secret,
        "credential_group": spec.credential_group,
        "pair_with": spec.pair_with,
        "input_kind": spec.input_kind,
        "requires_restart": spec.requires_restart,
        "test_kind": spec.test_kind,
        "batch": spec.batch,
        "configured": configured,
        "source": source,
        "readonly": readonly,
        "updated_at": updated_at,
    }
    if not spec.secret:
        # Vault error hides empty/unknown App ID; vault_readonly echoes DALTON_FEISHU_APP_ID.
        entry["value"] = None if source == "vault_error" else effective_val
    return entry


@router.get("")
async def get_deployment_settings_endpoint(
    user_id: AdminSessionUserId,
) -> JSONResponse:
    """Return profile-filtered deployment credential metadata, sources, and callback URLs."""
    _ = user_id
    profile = resolve_profile()
    revision, saved_rows, updated_at_map = await read_settings_snapshot_detailed()
    feishu_vault_pair, feishu_vault_error = await preload_feishu_vault_pair(saved_rows)

    fields = [
        _resolve_field_view(
            spec,
            saved_rows=saved_rows,
            updated_at_map=updated_at_map,
            feishu_vault_pair=feishu_vault_pair,
            feishu_vault_error=feishu_vault_error,
        )
        for spec in get_field_specs(profile=profile)
    ]

    public_url = get_dalton_public_app_url()
    google_eligible, google_reason = google_redirect_uri_eligible(public_url)

    return JSONResponse(
        status_code=200,
        content={
            "profile": profile.value,
            "revision": revision,
            "encryption_configured": encryption_configured(),
            "fields": fields,
            "redirect_uris": {
                "google": build_integration_redirect_uri("google"),
                "feishu": build_integration_redirect_uri("feishu"),
            },
            "feishu_events_url": build_feishu_integration_events_url(),
            "google_redirect_uri_eligible": google_eligible,
            "google_redirect_uri_reason": google_reason,
        },
        headers=_NO_STORE_HEADERS,
    )


@router.put("")
async def put_deployment_settings_endpoint(
    request: Request,
    body: DeploymentSettingsUpdateRequest,
    user_id: AdminSessionUserId,
) -> JSONResponse:
    """Atomically save or clear allowlisted deployment credentials and return the new revision."""
    result = await write_settings(
        body.values,
        expected_revision=body.expected_revision,
        actor_user_id=user_id,
        client_ip=extract_client_ip(request),
        profile=resolve_profile(),
    )
    return JSONResponse(
        status_code=200,
        content={
            "revision": result.revision,
            "changed": list(result.changed_names),
            "pending_apply": result.pending_apply,
            "warnings": list(result.warnings),
        },
        headers=_NO_STORE_HEADERS,
    )


@router.post("/test")
async def test_deployment_settings_endpoint(
    body: DeploymentSettingsTestRequest,
    user_id: AdminSessionUserId,
) -> JSONResponse:
    """Run a live official-endpoint credential probe without logging or returning secrets."""
    _ = user_id
    probe_result = await run_credential_probe(
        body.target,
        body.values,
        profile=resolve_profile(),
    )
    return JSONResponse(
        status_code=200,
        content=probe_result.to_dict(),
        headers=_NO_STORE_HEADERS,
    )
