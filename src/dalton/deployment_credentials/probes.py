"""Real-endpoint credential probes for deployment-level integrations (§3.2).

Distinguishes network reachability from client credential validity without ever
logging, caching, or returning secrets or issued access tokens:
- ``google_oauth``: POSTs to Google's official token endpoint with a fixed dummy
  authorization code and the standard callback URI; ``invalid_grant`` proves
  Google authenticated the client ID & secret while ``invalid_client`` /
  ``unauthorized_client`` proves invalid credentials.
- ``feishu_app``: POSTs to Feishu's official ``tenant_access_token/internal``
  endpoint; ``code == 0`` proves valid credentials and immediately discards the
  returned ``tenant_access_token``; distinguishes ``10003`` (invalid param),
  ``10014`` (app unavailable), and secret/credential errors.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from src.dalton.deployment_credentials.catalog import (
    FEISHU_APP_ID_KEY,
    FEISHU_APP_SECRET_KEY,
    GOOGLE_OAUTH_CLIENT_ID_KEY,
    GOOGLE_OAUTH_CLIENT_SECRET_KEY,
    group_keys,
)
from src.dalton.deployment_credentials.snapshot import (
    _validate_credential_value,
    query_feishu_vault_pair_strict,
)
from src.dalton.deployment_credentials.store import (
    _MAX_VALUE_LENGTH,
    DeploymentSettingValidationError,
    read_settings_snapshot,
)
from src.dalton.integrations.constants import (
    GOOGLE_OAUTH_CALLBACK_PATH,
    build_integration_redirect_uri,
)
from src.dalton.profile import DeploymentProfile, resolve_profile
from src.server.services.setup_settings import environment as setup_env

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
FEISHU_TENANT_TOKEN_ENDPOINT = (
    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
)
GOOGLE_PROBE_INVALID_CODE = "dalton_probe_invalid_code"
PROBE_TIMEOUT_SECONDS = 8.0

ProbeTarget = Literal["google_oauth", "feishu_app"]
ProbeVerdict = Literal[
    "valid",
    "invalid_credentials",
    "network_error",
    "rate_limited",
    "format_invalid",
]

_FEISHU_SECRET_ERROR_CODES: frozenset[int] = frozenset(
    {10004, 10012, 10013, 20001, 20003, 20004}
)


@dataclass(frozen=True)
class ProbeResult:
    """Structured probe result containing zero secrets or upstream tokens."""

    ok: bool
    network_reachable: bool
    credentials_valid: bool | None
    verdict: ProbeVerdict
    detail_code: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "network_reachable": self.network_reachable,
            "credentials_valid": self.credentials_valid,
            "verdict": self.verdict,
            "detail_code": self.detail_code,
        }


def create_probe_http_client() -> httpx.AsyncClient:
    """Build the default outbound HTTP client for fixed official vendor probes."""
    return httpx.AsyncClient(
        timeout=PROBE_TIMEOUT_SECONDS,
        follow_redirects=False,
    )


async def _resolve_existing_group_values(target: ProbeTarget) -> tuple[dict[str, str], bool]:
    """Resolve current effective values for ``target`` using §2.3 single-source precedence.

    Returns ``(values_dict, vault_error)``. Reads fresh DB snapshot when startup
    env does not own the group so probes reflect newly committed settings even
    before local worker refresh completes.
    """
    keys = group_keys(target)
    at_start = setup_env._snapshot()
    if any(setup_env.set_outside_app(k) for k in keys):
        return (
            {k: str(at_start[k]) for k in keys if isinstance(at_start.get(k), str) and at_start[k]},
            False,
        )

    _revision, saved_rows = await read_settings_snapshot()
    if any(k in saved_rows for k in keys):
        return ({k: saved_rows[k] for k in keys if k in saved_rows}, False)

    if target == "feishu_app":
        vault_pair, vault_error, _err = await query_feishu_vault_pair_strict()
        if vault_error:
            return {}, True
        if vault_pair is not None:
            return ({k: vault_pair[k] for k in keys if k in vault_pair}, False)

    return {}, False


def _validate_draft_values(
    target: ProbeTarget,
    draft_values: Mapping[str, Any],
) -> tuple[dict[str, str], ProbeResult | None]:
    """Validate optional draft inputs against ``target``'s allowlisted keys.

    Unknown keys raise ``DeploymentSettingValidationError`` with ``_unknown`` so
    neither unrecognized key names nor raw values echo back in 422 responses.
    """
    allowed = set(group_keys(target))
    unknown_errors: list[dict[str, str]] = []
    cleaned_draft: dict[str, str] = {}

    for key, raw in draft_values.items():
        if key not in allowed:
            unknown_errors.append({"setting_name": "_unknown", "reason": "unknown_setting"})
            continue
        if raw is None:
            continue
        if not isinstance(raw, str):
            return {}, ProbeResult(
                ok=False,
                network_reachable=False,
                credentials_valid=None,
                verdict="format_invalid",
                detail_code="invalid_format",
            )
        stripped = raw.strip()
        if not stripped:
            continue
        if len(stripped) > _MAX_VALUE_LENGTH:
            return {}, ProbeResult(
                ok=False,
                network_reachable=False,
                credentials_valid=None,
                verdict="format_invalid",
                detail_code="value_too_long",
            )
        cleaned, is_invalid = _validate_credential_value(stripped)
        if is_invalid or cleaned is None:
            return {}, ProbeResult(
                ok=False,
                network_reachable=False,
                credentials_valid=None,
                verdict="format_invalid",
                detail_code="invalid_format",
            )
        cleaned_draft[key] = cleaned

    if unknown_errors:
        raise DeploymentSettingValidationError(unknown_errors)

    return cleaned_draft, None


async def probe_google_oauth(
    client_id: str,
    client_secret: str,
    *,
    redirect_uri: str,
    http_client: httpx.AsyncClient | None = None,
) -> ProbeResult:
    """Verify Google OAuth client credentials against Google's official token endpoint."""
    form_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": GOOGLE_PROBE_INVALID_CODE,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            if http_client is not None:
                response = await http_client.post(
                    GOOGLE_TOKEN_ENDPOINT,
                    data=form_data,
                    headers={"Accept": "application/json"},
                )
            else:
                async with create_probe_http_client() as owned_client:
                    response = await owned_client.post(
                        GOOGLE_TOKEN_ENDPOINT,
                        data=form_data,
                        headers={"Accept": "application/json"},
                    )
    except (TimeoutError, httpx.TimeoutException) as exc:
        logger.info(
            "Deployment credential probe failed target=google_oauth error=%s",
            type(exc).__name__,
        )
        return ProbeResult(
            ok=False,
            network_reachable=False,
            credentials_valid=None,
            verdict="network_error",
            detail_code="network_timeout",
        )
    except httpx.HTTPError as exc:
        logger.info(
            "Deployment credential probe failed target=google_oauth error=%s",
            type(exc).__name__,
        )
        return ProbeResult(
            ok=False,
            network_reachable=False,
            credentials_valid=None,
            verdict="network_error",
            detail_code="network_error",
        )

    status_code = response.status_code
    error_code = ""
    try:
        payload = response.json()
        if isinstance(payload, Mapping):
            error_code = str(payload.get("error") or "").strip().lower()
    except ValueError:
        error_code = ""

    if status_code == 429:
        result = ProbeResult(
            ok=False,
            network_reachable=True,
            credentials_valid=None,
            verdict="rate_limited",
            detail_code="rate_limited",
        )
    elif status_code == 401 or error_code in ("invalid_client", "unauthorized_client"):
        detail = (
            error_code
            if error_code in ("invalid_client", "unauthorized_client")
            else "invalid_client"
        )
        result = ProbeResult(
            ok=False,
            network_reachable=True,
            credentials_valid=False,
            verdict="invalid_credentials",
            detail_code=detail,
        )
    elif status_code == 400 and error_code == "invalid_grant":
        result = ProbeResult(
            ok=True,
            network_reachable=True,
            credentials_valid=True,
            verdict="valid",
            detail_code="invalid_grant_verified",
        )
    elif status_code >= 500:
        result = ProbeResult(
            ok=False,
            network_reachable=True,
            credentials_valid=None,
            verdict="network_error",
            detail_code=f"upstream_http_{status_code}",
        )
    else:
        detail = (
            error_code
            if error_code in ("invalid_request", "unsupported_grant_type", "access_denied")
            else f"http_{status_code}"
        )
        result = ProbeResult(
            ok=False,
            network_reachable=True,
            credentials_valid=False,
            verdict="invalid_credentials",
            detail_code=detail,
        )

    logger.info(
        "Deployment credential probe finished target=google_oauth verdict=%s detail_code=%s",
        result.verdict,
        result.detail_code,
    )
    return result


async def probe_feishu_app(
    app_id: str,
    app_secret: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> ProbeResult:
    """Verify Feishu self-built app credentials against ``tenant_access_token/internal``.

    When Feishu returns ``code == 0``, the issued ``tenant_access_token`` is
    discarded immediately without caching, logging, or returning it.
    """
    body = {"app_id": app_id, "app_secret": app_secret}
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            if http_client is not None:
                response = await http_client.post(
                    FEISHU_TENANT_TOKEN_ENDPOINT,
                    json=body,
                    headers={"Accept": "application/json"},
                )
            else:
                async with create_probe_http_client() as owned_client:
                    response = await owned_client.post(
                        FEISHU_TENANT_TOKEN_ENDPOINT,
                        json=body,
                        headers={"Accept": "application/json"},
                    )
    except (TimeoutError, httpx.TimeoutException) as exc:
        logger.info(
            "Deployment credential probe failed target=feishu_app error=%s",
            type(exc).__name__,
        )
        return ProbeResult(
            ok=False,
            network_reachable=False,
            credentials_valid=None,
            verdict="network_error",
            detail_code="network_timeout",
        )
    except httpx.HTTPError as exc:
        logger.info(
            "Deployment credential probe failed target=feishu_app error=%s",
            type(exc).__name__,
        )
        return ProbeResult(
            ok=False,
            network_reachable=False,
            credentials_valid=None,
            verdict="network_error",
            detail_code="network_error",
        )

    status_code = response.status_code
    feishu_code: int | None = None
    try:
        payload = response.json()
        if isinstance(payload, Mapping):
            raw_code = payload.get("code")
            if isinstance(raw_code, int) and not isinstance(raw_code, bool):
                feishu_code = raw_code
        # Drop reference to the parsed payload immediately so tenant_access_token is never retained.
        payload = None
    except ValueError:
        feishu_code = None

    if status_code == 429 or feishu_code == 99991400:
        result = ProbeResult(
            ok=False,
            network_reachable=True,
            credentials_valid=None,
            verdict="rate_limited",
            detail_code="rate_limited",
        )
    elif status_code == 200 and feishu_code == 0:
        result = ProbeResult(
            ok=True,
            network_reachable=True,
            credentials_valid=True,
            verdict="valid",
            detail_code="feishu_token_verified",
        )
    elif feishu_code == 10003:
        result = ProbeResult(
            ok=False,
            network_reachable=True,
            credentials_valid=False,
            verdict="invalid_credentials",
            detail_code="feishu_invalid_param",
        )
    elif feishu_code == 10014:
        result = ProbeResult(
            ok=False,
            network_reachable=True,
            credentials_valid=False,
            verdict="invalid_credentials",
            detail_code="feishu_app_unavailable",
        )
    elif feishu_code in _FEISHU_SECRET_ERROR_CODES:
        result = ProbeResult(
            ok=False,
            network_reachable=True,
            credentials_valid=False,
            verdict="invalid_credentials",
            detail_code="feishu_invalid_secret",
        )
    elif status_code >= 500:
        result = ProbeResult(
            ok=False,
            network_reachable=True,
            credentials_valid=None,
            verdict="network_error",
            detail_code=f"upstream_http_{status_code}",
        )
    elif feishu_code is not None or status_code in (400, 401, 403):
        result = ProbeResult(
            ok=False,
            network_reachable=True,
            credentials_valid=False,
            verdict="invalid_credentials",
            detail_code="feishu_invalid_credentials",
        )
    else:
        result = ProbeResult(
            ok=False,
            network_reachable=True,
            credentials_valid=None,
            verdict="network_error",
            detail_code=f"http_{status_code}",
        )

    logger.info(
        "Deployment credential probe finished target=feishu_app verdict=%s detail_code=%s",
        result.verdict,
        result.detail_code,
    )
    return result


async def run_credential_probe(
    target: ProbeTarget,
    draft_values: Mapping[str, Any] | None = None,
    *,
    profile: DeploymentProfile | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> ProbeResult:
    """Resolve effective or draft credentials for ``target`` and run its real-endpoint probe."""
    _ = resolve_profile() if profile is None else profile
    cleaned_draft, format_error = _validate_draft_values(target, draft_values or {})
    if format_error is not None:
        return format_error

    keys = group_keys(target)
    if all(k in cleaned_draft for k in keys):
        effective = dict(cleaned_draft)
    else:
        existing, vault_error = await _resolve_existing_group_values(target)
        if vault_error and not cleaned_draft:
            return ProbeResult(
                ok=False,
                network_reachable=False,
                credentials_valid=None,
                verdict="format_invalid",
                detail_code="vault_error",
            )
        effective = dict(existing)
        effective.update(cleaned_draft)

    resolved: dict[str, str] = {}
    for key in keys:
        cleaned, is_invalid = _validate_credential_value(effective.get(key))
        if is_invalid:
            return ProbeResult(
                ok=False,
                network_reachable=False,
                credentials_valid=None,
                verdict="format_invalid",
                detail_code="invalid_format",
            )
        if cleaned is None:
            return ProbeResult(
                ok=False,
                network_reachable=False,
                credentials_valid=None,
                verdict="format_invalid",
                detail_code="incomplete_credential_group",
            )
        resolved[key] = cleaned

    if target == "google_oauth":
        redirect_uri = (
            build_integration_redirect_uri("google")
            or f"http://localhost{GOOGLE_OAUTH_CALLBACK_PATH}"
        )
        return await probe_google_oauth(
            resolved[GOOGLE_OAUTH_CLIENT_ID_KEY],
            resolved[GOOGLE_OAUTH_CLIENT_SECRET_KEY],
            redirect_uri=redirect_uri,
            http_client=http_client,
        )

    return await probe_feishu_app(
        resolved[FEISHU_APP_ID_KEY],
        resolved[FEISHU_APP_SECRET_KEY],
        http_client=http_client,
    )


__all__ = [
    "FEISHU_TENANT_TOKEN_ENDPOINT",
    "GOOGLE_PROBE_INVALID_CODE",
    "GOOGLE_TOKEN_ENDPOINT",
    "PROBE_TIMEOUT_SECONDS",
    "ProbeResult",
    "ProbeTarget",
    "ProbeVerdict",
    "create_probe_http_client",
    "probe_feishu_app",
    "probe_google_oauth",
    "run_credential_probe",
]
