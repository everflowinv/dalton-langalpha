"""Personal integration OAuth connect, callback, confirmation, and serialized CAS refresh (§4.2).

Deployment credentials are read exclusively via ``get_credential_group(...)``.
All concurrency state (one-time OAuth ``state``, pending confirmations, lifecycle /
refresh / revoke advisory locks) lives in PostgreSQL for multi-worker safety.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import logging
import secrets
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import uuid4

import anyio
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.config.settings import LOCAL_DEV_USER_ID
from src.dalton.auth.sessions import verify_local_session_active
from src.dalton.capabilities import (
    CapabilityId,
    has_capability,
    require_personal_integration_capability,
)
from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import (
    IntegrationRow,
    IntegrationStatus,
    LocalPurgeStatus,
    OAuthIntent,
    RemoteRevokeStatus,
    TriggerReason,
    extract_google_project,
    lifecycle_lock_key,
    refresh_lock_key,
    revoke_lock_key,
)
from src.dalton.deployment_credentials import resolve_feishu_app_credentials
from src.dalton.deployment_credentials.catalog import (
    FEISHU_APP_ID_KEY,
    FEISHU_APP_SECRET_KEY,
    GOOGLE_OAUTH_CLIENT_ID_KEY,
    GOOGLE_OAUTH_CLIENT_SECRET_KEY,
)
from src.dalton.deployment_credentials.redirect_uri import google_redirect_uri_eligible
from src.dalton.deployment_credentials.snapshot import get_credential_group
from src.dalton.integrations.constants import (
    DEFAULT_INTEGRATIONS_RETURN_TO,
    FEISHU_CALENDAR_SCOPES,
    FEISHU_MAIL_READONLY_SCOPES,
    FEISHU_MAIL_SEND_SCOPE,
    GOOGLE_CALENDAR_MIN_SCOPES,
    GOOGLE_GMAIL_DRAFT_SCOPES,
    GOOGLE_GMAIL_MIN_SCOPES,
    GOOGLE_IDENTITY_SCOPES,
    GOOGLE_SCOPE_GMAIL_DRAFTS_CREATE,
    OAUTH_STATE_TTL_SECONDS,
    PENDING_CONFIRMATION_TTL_SECONDS,
    build_integration_redirect_uri,
    get_connector_spec,
)
from src.dalton.integrations.oauth_clients import (
    FEISHU_AUTHORIZE_URL,
    GOOGLE_AUTHORIZE_URL,
    FeishuOAuthClientProtocol,
    GoogleIdTokenVerificationError,
    GoogleOAuthClientProtocol,
    OAuthTokenResponse,
    OAuthUpstreamError,
    get_feishu_oauth_client,
    get_google_oauth_client,
    get_google_remote_revoker,
)
from src.dalton.invites import is_team_member
from src.dalton.services.integration_revoke import (
    RemoteRevoker,
    RevokeOutcome,
    compute_revoke_backoff_seconds,
    revoke_before_new_authorization,
)
from src.dalton.services.user_integrations_lifecycle import (
    AccountSwitchTargetChangedError,
    DisconnectTargetMismatchError,
    IntegrationNotFoundError,
    MissingRefreshTokenOnAccountSwitchError,
    begin_disconnect,
    finish_disconnect,
    finish_disconnect_locked,
    replace_connection_for_account_switch,
)
from src.dalton.settings import get_dalton_auth_mode, get_dalton_public_app_url
from src.server.database.encryption import encryption_configured, get_encryption_key
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_DEFAULT_WAITER_BUDGET_SECONDS = 12.0
_DEFAULT_WAITER_POLL_INTERVAL_SECONDS = 0.2
_FEISHU_DB_COMMIT_RETRIES = 3

OAUTH_BROWSER_BINDING_COOKIE_NAME = "dalton_oauth_bind"

_ALLOWED_RETURN_TO_EXACT: frozenset[str] = frozenset(
    {
        "/",
        "/settings",
        "/dashboard",
        "/chat",
        "/market",
        "/automations",
        "/monitors",
        "/plugins",
        "/connectors",
    }
)
_ALLOWED_RETURN_TO_PREFIXES: tuple[str, ...] = (
    "/settings/",
    "/chat/",
    "/monitors/",
)


# ---------------------------------------------------------------------------
# Typed domain exceptions (safe messages; zero secret values)
# ---------------------------------------------------------------------------


class PersonalIntegrationError(RuntimeError):
    """Base error for personal integration OAuth and refresh flows."""

    status_code: int = 400
    code: str = "integration_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details: dict[str, Any] = dict(details or {})


class IntegrationCredentialsNotReadyError(PersonalIntegrationError):
    status_code = 503
    code = "personal_integrations_not_ready"


class OAuthStateInvalidError(PersonalIntegrationError):
    status_code = 400
    code = "invalid_or_expired_state"


class OAuthSessionInvalidError(PersonalIntegrationError):
    status_code = 401
    code = "session_invalid"


class MissingRequiredScopesError(PersonalIntegrationError):
    status_code = 400
    code = "missing_required_scopes"


class MissingRefreshTokenOnClientRotationError(PersonalIntegrationError):
    status_code = 400
    code = "missing_refresh_token_on_client_rotation"


class MissingRefreshTokenError(PersonalIntegrationError):
    status_code = 400
    code = "missing_refresh_token"


class FeishuIdentityMismatchError(PersonalIntegrationError):
    status_code = 403
    code = "feishu_identity_mismatch"


class PendingConfirmationInvalidError(PersonalIntegrationError):
    status_code = 400
    code = "invalid_or_expired_confirmation"


class IntegrationNotConnectedError(PersonalIntegrationError):
    status_code = 404
    code = "integration_not_connected"


class IntegrationEpochRevokedError(PersonalIntegrationError):
    status_code = 403
    code = "integration_epoch_revoked"


class IntegrationNeedsReauthError(PersonalIntegrationError):
    status_code = 401
    code = "needs_reauth"


class IntegrationRevokedError(PersonalIntegrationError):
    status_code = 403
    code = "integration_revoked"


class IntegrationRefreshBusyError(PersonalIntegrationError):
    status_code = 503
    code = "refresh_in_progress"


class IntegrationTransientError(PersonalIntegrationError):
    status_code = 503
    code = "upstream_transient_error"


# ---------------------------------------------------------------------------
# Safe result dataclasses (tokens & confirmations excluded from repr)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StartAuthorizationResult:
    authorize_url: str = field(repr=False)
    provider: str
    service: str
    intent: str
    expires_in: int = OAUTH_STATE_TTL_SECONDS
    state: str = field(default="", repr=False)
    browser_binding_cookie: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class OAuthCallbackResult:
    status: str  # connected | confirmation_required | account_switch_confirmation_required | error
    provider: str
    service: str
    return_to: str
    connection: IntegrationRow | None = None
    confirmation_token: str | None = field(default=None, repr=False)
    previous_account_email: str | None = None
    new_account_email: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    partial_grant: bool = False


@dataclass(frozen=True, slots=True)
class ConfirmIntegrationResult:
    status: str  # connected | account_switch_confirmation_required
    provider: str
    service: str
    connection: IntegrationRow | None = None
    purge_id: str | None = None
    previous_account_email: str | None = None
    new_account_email: str | None = None


@dataclass(frozen=True, slots=True)
class FreshAccessToken:
    access_token: str = field(repr=False)
    provider: str
    service: str
    oauth_client_id: str
    provider_account_id: str
    auth_epoch: int
    token_generation: int
    granted_scopes: str
    expires_at: datetime | None


# ---------------------------------------------------------------------------
# Helpers: credentials, PKCE, scopes, return_to validation
# ---------------------------------------------------------------------------


def _sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _has_unsafe_url_chars(value: str) -> bool:
    return any(
        ord(ch) < 0x20 or ord(ch) == 0x7F or ch == "\\"
        for ch in value
    )


def _normalize_browser_path(raw_path: str) -> str | None:
    """Normalize a relative URL path following WHATWG URL dot-segment rules.

    Resolves single-dot (``.``, ``%2e``) and double-dot (``..``, ``.%2e``, ``%2e.``,
    ``%2e%2e``) segments case-insensitively and rejects control bytes, backslashes,
    percent-encoded slashes/backslashes/controls, and protocol-relative outputs.
    """
    if not raw_path or not raw_path.startswith("/") or raw_path.startswith("//"):
        return None
    if "\\" in raw_path or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in raw_path):
        return None
    lower_path = raw_path.lower()
    if "%5c" in lower_path or "%2f" in lower_path:
        return None

    idx = 0
    length = len(raw_path)
    while idx < length:
        if raw_path[idx] == "%":
            if idx + 2 >= length:
                return None
            try:
                byte_val = int(raw_path[idx + 1 : idx + 3], 16)
            except ValueError:
                return None
            if byte_val < 0x20 or byte_val == 0x7F:
                return None
            idx += 3
        else:
            idx += 1

    single_dot = frozenset({".", "%2e"})
    double_dot = frozenset({"..", ".%2e", "%2e.", "%2e%2e"})
    trailing_slash = raw_path.endswith("/")
    stack: list[str] = []
    raw_segments = raw_path.split("/")[1:]
    for pos, seg in enumerate(raw_segments):
        seg_lower = seg.lower()
        is_last = pos == len(raw_segments) - 1
        if seg_lower in double_dot:
            if stack:
                stack.pop()
            if is_last:
                trailing_slash = True
        elif seg_lower in single_dot:
            if is_last:
                trailing_slash = True
        elif seg == "" and is_last:
            continue
        else:
            stack.append(seg)

    if not stack:
        return "/"
    normalized = "/" + "/".join(stack)
    if trailing_slash and not normalized.endswith("/"):
        normalized += "/"
    if not normalized.startswith("/") or normalized.startswith("//"):
        return None
    check_parts = urlsplit(normalized)
    if check_parts.scheme or check_parts.netloc:
        return None
    return normalized


def _is_known_in_app_path(path: str) -> bool:
    normalized = _normalize_browser_path(path)
    if normalized is None:
        return False
    exact_check = normalized.rstrip("/") or "/"
    if exact_check in _ALLOWED_RETURN_TO_EXACT:
        return True
    return any(normalized.startswith(prefix) for prefix in _ALLOWED_RETURN_TO_PREFIXES)


def sanitize_return_to(raw: str | None) -> str:
    """Normalize ``return_to`` by browser URL rules and allow only fixed in-app routes."""
    if not isinstance(raw, str):
        return DEFAULT_INTEGRATIONS_RETURN_TO
    if _has_unsafe_url_chars(raw):
        return DEFAULT_INTEGRATIONS_RETURN_TO
    cleaned = raw.strip()
    if not cleaned or not cleaned.startswith("/") or cleaned.startswith("//"):
        return DEFAULT_INTEGRATIONS_RETURN_TO
    parts = urlsplit(cleaned)
    if parts.scheme or parts.netloc:
        return DEFAULT_INTEGRATIONS_RETURN_TO
    normalized_path = _normalize_browser_path(parts.path)
    if normalized_path is None or not _is_known_in_app_path(normalized_path):
        return DEFAULT_INTEGRATIONS_RETURN_TO
    return urlunsplit(("", "", normalized_path, parts.query, parts.fragment))


_sanitize_return_to = sanitize_return_to


def validate_callback_location(location: str) -> bool:
    """Verify a final redirect ``Location`` header stays strictly on a canonical in-app route."""
    if not isinstance(location, str) or not location:
        return False
    if _has_unsafe_url_chars(location):
        return False
    if not location.startswith("/") or location.startswith("//"):
        return False
    parts = urlsplit(location)
    if parts.scheme or parts.netloc:
        return False
    normalized_path = _normalize_browser_path(parts.path)
    if normalized_path is None or normalized_path != parts.path:
        return False
    return _is_known_in_app_path(normalized_path)


async def _resolve_channel_gateway_feishu_app_id() -> str | None:
    """Return the Feishu App ID used by the production channel gateway, or None when disabled/unavailable (§4.3 item 2).

    Reads via the exact same ``resolve_feishu_app_credentials()`` contract used by
    ``POST /api/v1/dalton/channels/internal/data-task-notifications/feishu-credentials``.
    When ``CHANNEL_FEISHU`` is enabled but the gateway's App ID cannot be resolved, logs
    a warning and returns ``None`` so ``open_id`` comparison is skipped safely.
    """
    if not has_capability(CapabilityId.CHANNEL_FEISHU):
        return None
    try:
        feishu_group = await resolve_feishu_app_credentials()
    except Exception as exc:
        logger.warning(
            "[integration-oauth] unable to resolve channel gateway Feishu App ID (%s); skipping open_id check",
            type(exc).__name__,
        )
        return None
    if not feishu_group.ready:
        logger.warning(
            "[integration-oauth] channel gateway Feishu credentials not ready (source=%s); skipping open_id check",
            feishu_group.source,
        )
        return None
    app_id = str(feishu_group.values.get(FEISHU_APP_ID_KEY) or "").strip() or None
    if not app_id:
        logger.warning(
            "[integration-oauth] channel gateway Feishu App ID unavailable (source=%s); skipping open_id check",
            feishu_group.source,
        )
        return None
    return app_id


def _parse_scopes(scope_str: str | None) -> list[str]:
    if not scope_str:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for tok in str(scope_str).replace(",", " ").split():
        s = tok.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _format_scopes(scopes: Collection[str]) -> str:
    return " ".join(sorted({s.strip() for s in scopes if s and s.strip()}))


def _load_provider_oauth_credentials(provider: str) -> tuple[str, str]:
    """Read provider OAuth ``(client_id, client_secret)`` exclusively via ``get_credential_group``."""
    norm = str(provider or "").strip().lower()
    if norm == "google":
        group = get_credential_group("google_oauth")
        if not group.ready:
            raise IntegrationCredentialsNotReadyError(
                "管理员尚未配置 Google OAuth 客户端凭据，请先在「部署设置 → 集成凭据」配置后再连接。",
                details={"provider": "google", "group": "google_oauth", "source": group.source},
            )
        return (
            group.values[GOOGLE_OAUTH_CLIENT_ID_KEY],
            group.values[GOOGLE_OAUTH_CLIENT_SECRET_KEY],
        )
    if norm == "feishu":
        group = get_credential_group("feishu_app")
        if not group.ready:
            raise IntegrationCredentialsNotReadyError(
                "管理员尚未配置飞书应用凭据，请先在「部署设置 → 集成凭据」配置后再连接。",
                details={"provider": "feishu", "group": "feishu_app", "source": group.source},
            )
        return (
            group.values[FEISHU_APP_ID_KEY],
            group.values[FEISHU_APP_SECRET_KEY],
        )
    raise PersonalIntegrationError(f"Unsupported provider: {provider!r}", code="unsupported_provider")


def _ensure_redirect_uri_and_encryption(provider: str) -> str:
    if not encryption_configured():
        raise IntegrationCredentialsNotReadyError(
            "管理员尚未配置数据加密密钥（BYOK_ENCRYPTION_KEY），暂时无法保存个人集成凭据。",
            details={"provider": provider, "missing": ["encryption_key"]},
        )
    public_url = get_dalton_public_app_url().strip()
    if provider == "google":
        eligible, reason = google_redirect_uri_eligible(public_url)
        if not eligible:
            raise IntegrationCredentialsNotReadyError(
                "管理员尚未配置符合 Google OAuth 要求的公网回调地址（PUBLIC_APP_URL），暂时无法连接。",
                details={"provider": provider, "reason": reason or "https_required"},
            )
    redirect_uri = build_integration_redirect_uri(provider)
    if not redirect_uri:
        raise IntegrationCredentialsNotReadyError(
            "管理员尚未配置公网访问地址（PUBLIC_APP_URL），暂时无法生成 OAuth 回调地址。",
            details={"provider": provider, "missing": ["public_app_url"]},
        )
    return redirect_uri


def _missing_min_scopes(provider: str, service: str, granted: Collection[str]) -> list[str]:
    granted_set = set(granted)
    spec = get_connector_spec(provider, service)
    if spec is None:
        return ["unsupported_service"]
    if provider == "google":
        return [s for s in spec.min_required_scopes if s not in granted_set]
    # Feishu v2 token responses may omit offline_access from returned scope string even when refresh_token was issued
    required = [s for s in spec.min_required_scopes if s != "offline_access"]
    return [s for s in required if s not in granted_set]


def _converge_preferences_for_scopes(
    provider: str,
    service: str,
    granted_scopes: Collection[str],
    base_preferences: dict[str, Any] | None = None,
    *,
    requested_scopes: Collection[str] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Converge optional capability preferences to match actually granted scopes (§3.3, §4.2(1)).

    Returns ``(updated_preferences, partial_grant_flag)``.
    """
    prefs = dict(base_preferences or {})
    granted_set = set(granted_scopes)
    req_set = set(requested_scopes or ())
    partial_grant = False

    if provider == "google" and service == "mail":
        has_draft_scope = any(s in granted_set for s in GOOGLE_GMAIL_DRAFT_SCOPES)
        requested_draft = any(s in req_set for s in GOOGLE_GMAIL_DRAFT_SCOPES)
        if has_draft_scope:
            if requested_draft or prefs.get("allow_create_draft"):
                prefs["allow_create_draft"] = True
        else:
            if requested_draft or prefs.get("allow_create_draft"):
                partial_grant = True
            prefs["allow_create_draft"] = False
    elif provider == "feishu" and service == "mail":
        has_send_scope = FEISHU_MAIL_SEND_SCOPE in granted_set
        requested_send = FEISHU_MAIL_SEND_SCOPE in req_set
        if has_send_scope:
            if requested_send or prefs.get("allow_send_mail"):
                prefs["allow_send_mail"] = True
        else:
            if requested_send or prefs.get("allow_send_mail"):
                partial_grant = True
            prefs["allow_send_mail"] = False

    return prefs, partial_grant


# ---------------------------------------------------------------------------
# 1. Initiate OAuth Authorization (§4.2(1), §4.2(3))
# ---------------------------------------------------------------------------


async def _coordinate_pre_auth_google_revokes(
    *,
    user_id: str,
    service: str,
    target_client_id: str,
    switch_account: bool,
    existing_rows: list[IntegrationRow],
    revoker: RemoteRevoker,
) -> None:
    """Coordinate old Google grant revocation under ``(user_id, google_project, sub)`` before issuing state (§4.2(1)(3))."""
    target_project = extract_google_project(target_client_id)

    # 1. Process any pending / failed_retryable ledger tasks in the same Google project
    ledgers = await db.list_user_purge_ledgers(user_id, active_only=True, limit=100)
    pending_subs: set[str] = set()
    for ledger in ledgers:
        if (
            ledger.provider == "google"
            and ledger.remote_revoke_status
            in (RemoteRevokeStatus.PENDING, RemoteRevokeStatus.FAILED_RETRYABLE)
            and extract_google_project(ledger.oauth_client_id) == target_project
        ):
            pending_subs.add(ledger.provider_account_id)

    for sub in sorted(pending_subs):
        await revoke_before_new_authorization(user_id, target_project, sub, revoker)

    # 2. Client-rotation reconnect on same Google project when no other active sibling stays on old client
    if not switch_account:
        target_existing = next((r for r in existing_rows if r.service == service), None)
        if (
            target_existing is not None
            and target_existing.oauth_client_id != target_client_id
            and extract_google_project(target_existing.oauth_client_id) == target_project
        ):
            old_client_id = target_existing.oauth_client_id
            sub = target_existing.provider_account_id
            lkey = revoke_lock_key(user_id, target_project, sub)
            async with get_db_connection() as lock_conn:
                await lock_conn.execute("SELECT pg_advisory_lock(%s)", (lkey,))
                try:
                    # Re-check under revoke_lock: sibling still on old client OR any active/pending
                    # grant on a new client in the same (Google project, sub)
                    latest_rows = await db.list_user_connections(user_id, provider="google")
                    other_on_old = [
                        r
                        for r in latest_rows
                        if r.service != service
                        and r.oauth_client_id == old_client_id
                        and r.provider_account_id == sub
                        and r.status
                        not in (
                            IntegrationStatus.DISCONNECT_PENDING,
                            IntegrationStatus.REVOKED_PURGING,
                        )
                    ]
                    if other_on_old:
                        return

                    has_newer_on_project = await db.has_active_or_pending_google_grant(
                        user_id,
                        target_project,
                        sub,
                        exclude_client_id=old_client_id,
                    )
                    if has_newer_on_project:
                        # A new client in the same Google Cloud project already has a live/pending
                        # grant for this sub: revoking the old token would destroy the new grant!
                        async with get_db_connection() as sup_conn, sup_conn.transaction():
                            await db.supersede_active_revokes_for_google_project_sub_tx(
                                user_id,
                                target_project,
                                sub,
                                reason="superseded_by_active_project_grant",
                                conn=sup_conn,
                            )
                            await db.insert_purge_ledger(
                                purge_id=str(uuid4()),
                                user_id=user_id,
                                provider="google",
                                service=service,
                                oauth_client_id=old_client_id,
                                provider_account_id=sub,
                                auth_epoch_upto=target_existing.auth_epoch,
                                trigger_reason=TriggerReason.CLIENT_ROTATION,
                                local_purge_status=LocalPurgeStatus.NOT_REQUIRED,
                                purged_max_turn_by_thread={},
                                remote_revoke_status=RemoteRevokeStatus.SUPERSEDED,
                                encrypted_revoke_token=None,
                                conn=sup_conn,
                            )
                        return

                    full_row = await db.get_connection(
                        user_id, "google", service, decrypt_tokens=True
                    )
                    if (
                        full_row is None
                        or full_row.oauth_client_id != old_client_id
                        or full_row.provider_account_id != sub
                    ):
                        return
                    old_tok = full_row.refresh_token or full_row.access_token
                    if not old_tok:
                        return

                    try:
                        outcome = await revoker.revoke(
                            provider="google",
                            oauth_client_id=old_client_id,
                            token=old_tok,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[integration-oauth] pre-auth rotation revoke raised %s; queueing retry",
                            type(exc).__name__,
                        )
                        outcome = RevokeOutcome.RETRYABLE

                    async with get_db_connection() as rec_conn, rec_conn.transaction():
                        if outcome == RevokeOutcome.SUCCEEDED:
                            await db.insert_purge_ledger(
                                purge_id=str(uuid4()),
                                user_id=user_id,
                                provider="google",
                                service=service,
                                oauth_client_id=old_client_id,
                                provider_account_id=sub,
                                auth_epoch_upto=full_row.auth_epoch,
                                trigger_reason=TriggerReason.CLIENT_ROTATION,
                                local_purge_status=LocalPurgeStatus.NOT_REQUIRED,
                                purged_max_turn_by_thread={},
                                remote_revoke_status=RemoteRevokeStatus.SUCCEEDED,
                                encrypted_revoke_token=None,
                                conn=rec_conn,
                            )
                        elif outcome == RevokeOutcome.RETRYABLE:
                            # Persist encrypted token into the retry queue so abandoned reauths still revoke;
                            # completing the new OAuth flow will supersede this task under revoke_lock.
                            retry_pid = str(uuid4())
                            await db.insert_purge_ledger(
                                purge_id=retry_pid,
                                user_id=user_id,
                                provider="google",
                                service=service,
                                oauth_client_id=old_client_id,
                                provider_account_id=sub,
                                auth_epoch_upto=full_row.auth_epoch,
                                trigger_reason=TriggerReason.CLIENT_ROTATION,
                                local_purge_status=LocalPurgeStatus.NOT_REQUIRED,
                                purged_max_turn_by_thread={},
                                remote_revoke_status=RemoteRevokeStatus.PENDING,
                                revoke_token_plaintext=old_tok,
                                conn=rec_conn,
                            )
                            delay_s = compute_revoke_backoff_seconds(0)
                            await db.update_purge_ledger_remote_status(
                                retry_pid,
                                remote_revoke_status=RemoteRevokeStatus.FAILED_RETRYABLE,
                                clear_revoke_token=False,
                                increment_retry=True,
                                next_retry_at=datetime.now(UTC) + timedelta(seconds=delay_s),
                                last_remote_error="pre_auth_rotation_revoke_retryable",
                                mark_remote_completed=False,
                                conn=rec_conn,
                            )
                        else:
                            await db.insert_purge_ledger(
                                purge_id=str(uuid4()),
                                user_id=user_id,
                                provider="google",
                                service=service,
                                oauth_client_id=old_client_id,
                                provider_account_id=sub,
                                auth_epoch_upto=full_row.auth_epoch,
                                trigger_reason=TriggerReason.CLIENT_ROTATION,
                                local_purge_status=LocalPurgeStatus.NOT_REQUIRED,
                                purged_max_turn_by_thread={},
                                remote_revoke_status=RemoteRevokeStatus.SUPERSEDED,
                                encrypted_revoke_token=None,
                                conn=rec_conn,
                            )

                        await db.update_connection_status(
                            user_id,
                            "google",
                            service,
                            status=IntegrationStatus.NEEDS_REAUTH,
                            last_error_code="oauth_client_rotated",
                            clear_tokens=True,
                            expected_auth_epoch=full_row.auth_epoch,
                            conn=rec_conn,
                        )
                finally:
                    with anyio.CancelScope(shield=True):
                        await lock_conn.execute(
                            "SELECT pg_advisory_unlock(%s)", (lkey,)
                        )


async def start_oauth_authorization(
    *,
    user_id: str,
    provider: str,
    service: str,
    auth_mode: str,
    session_id: str | None = None,
    user_email: str | None = None,
    switch_account: bool = False,
    allow_create_draft: bool = False,
    allow_send_mail: bool = False,
    return_to: str | None = None,
    revoker: RemoteRevoker | None = None,
) -> StartAuthorizationResult:
    """Create a 10-minute single-use OAuth state with PKCE and return the provider authorize URL."""
    norm_provider = str(provider or "").strip().lower()
    norm_service = str(service or "").strip().lower()
    if get_connector_spec(norm_provider, norm_service) is None:
        raise PersonalIntegrationError(
            f"Unsupported integration: {norm_provider}/{norm_service}",
            code="unsupported_integration",
            status_code=404,
        )

    client_id, _client_secret = _load_provider_oauth_credentials(norm_provider)
    redirect_uri = _ensure_redirect_uri_and_encryption(norm_provider)
    safe_return_to = _sanitize_return_to(return_to)

    existing_rows = await db.list_user_connections(user_id, provider=norm_provider)
    target_existing = next((r for r in existing_rows if r.service == norm_service), None)

    if norm_provider == "google":
        eff_revoker = revoker or get_google_remote_revoker()
        await _coordinate_pre_auth_google_revokes(
            user_id=user_id,
            service=norm_service,
            target_client_id=client_id,
            switch_account=switch_account,
            existing_rows=existing_rows,
            revoker=eff_revoker,
        )

    code_verifier = secrets.token_urlsafe(48)
    code_challenge = _pkce_challenge(code_verifier)
    raw_state = secrets.token_urlsafe(32)
    state_hash = _sha256_hex(raw_state)
    expires_at = datetime.now(UTC) + timedelta(seconds=OAUTH_STATE_TTL_SECONDS)

    if norm_provider == "google":
        target_scopes: list[str] = list(GOOGLE_IDENTITY_SCOPES)
        if norm_service == "calendar":
            target_scopes.extend(GOOGLE_CALENDAR_MIN_SCOPES)
        else:
            target_scopes.extend(GOOGLE_GMAIL_MIN_SCOPES)
            if allow_create_draft:
                target_scopes.append(GOOGLE_SCOPE_GMAIL_DRAFTS_CREATE)

        same_client_active = [
            r
            for r in existing_rows
            if r.oauth_client_id == client_id
            and r.status in (IntegrationStatus.CONNECTED, IntegrationStatus.REFRESH_AMBIGUOUS)
        ]

        if not switch_account and same_client_active:
            merged_scopes: set[str] = set(target_scopes)
            for row in same_client_active:
                merged_scopes.update(_parse_scopes(row.granted_scopes))
            requested_scope_str = _format_scopes(merged_scopes)
            include_granted = "true"
            prompt = "consent"
            intent = (
                OAuthIntent.REAUTH.value
                if target_existing is not None
                and target_existing.status != IntegrationStatus.CONNECTED
                else OAuthIntent.CONNECT.value
            )
        else:
            requested_scope_str = _format_scopes(target_scopes)
            include_granted = "false"
            prompt = "select_account consent"
            if switch_account:
                intent = OAuthIntent.SWITCH_ACCOUNT.value
            elif target_existing is not None:
                intent = OAuthIntent.REAUTH.value
            else:
                intent = OAuthIntent.CONNECT.value

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": requested_scope_str,
            "access_type": "offline",
            "include_granted_scopes": include_granted,
            "prompt": prompt,
            "state": raw_state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        authorize_url = f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"
    else:
        if norm_service == "calendar":
            feishu_scopes = list(FEISHU_CALENDAR_SCOPES)
        else:
            feishu_scopes = list(FEISHU_MAIL_READONLY_SCOPES)
            if allow_send_mail:
                feishu_scopes.append(FEISHU_MAIL_SEND_SCOPE)
        requested_scope_str = _format_scopes(feishu_scopes)
        if switch_account:
            intent = OAuthIntent.SWITCH_ACCOUNT.value
        elif target_existing is not None:
            intent = OAuthIntent.REAUTH.value
        else:
            intent = OAuthIntent.CONNECT.value

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": requested_scope_str,
            "state": raw_state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        authorize_url = f"{FEISHU_AUTHORIZE_URL}?{urlencode(params)}"

    browser_binding_cookie: str | None = None
    browser_binding_hash: str | None = None
    if auth_mode == "local":
        browser_binding_cookie = secrets.token_urlsafe(32)
        browser_binding_hash = _sha256_hex(browser_binding_cookie)

    await db.insert_oauth_state(
        state_hash=state_hash,
        user_id=user_id,
        auth_mode=auth_mode,
        session_id=session_id,
        user_email=user_email,
        provider=norm_provider,
        service=norm_service,
        oauth_client_id=client_id,
        intent=intent,
        code_verifier=code_verifier,
        requested_scopes=requested_scope_str,
        redirect_uri=redirect_uri,
        return_to=safe_return_to,
        expires_at=expires_at,
        browser_binding_hash=browser_binding_hash,
    )

    return StartAuthorizationResult(
        authorize_url=authorize_url,
        provider=norm_provider,
        service=norm_service,
        intent=intent,
        expires_in=OAUTH_STATE_TTL_SECONDS,
        state=raw_state,
        browser_binding_cookie=browser_binding_cookie,
    )


# ---------------------------------------------------------------------------
# 2. OAuth Callback (No Bearer Header) & Post-Callback Confirmation (§4.2(1))
# ---------------------------------------------------------------------------


async def _verify_original_session_for_callback(
    *,
    auth_mode: str,
    user_id: str,
    session_id: str | None,
    user_email: str | None,
) -> None:
    """Verify original session validity according to ``state_row.auth_mode`` (§4.2(1) item 2)."""
    current_mode = get_dalton_auth_mode()
    if auth_mode != current_mode:
        raise OAuthSessionInvalidError(
            "Authentication mode changed since OAuth authorization started.",
            code="auth_mode_mismatch",
            status_code=401,
        )
    if auth_mode == "local":
        if not session_id:
            raise OAuthSessionInvalidError(
                "Missing local session identifier on OAuth state.",
                code="session_invalid",
                status_code=401,
            )
        try:
            await verify_local_session_active(user_id=user_id, session_id=session_id)
        except HTTPException as exc:
            raise OAuthSessionInvalidError(
                "Original login session is no longer active.",
                code="session_invalid",
                status_code=exc.status_code,
            ) from None
        return

    if auth_mode == "oss":
        if user_id != LOCAL_DEV_USER_ID:
            raise OAuthSessionInvalidError(
                "OAuth state owner does not match the active OSS principal.",
                code="session_user_mismatch",
                status_code=403,
            )
        return

    if auth_mode == "supabase":
        cleaned_email = str(user_email or "").strip().lower()
        if not cleaned_email or not await is_team_member(cleaned_email):
            raise OAuthSessionInvalidError(
                "Account is no longer on the team allowlist.",
                code="team_membership_revoked",
                status_code=403,
            )
        return

    raise OAuthSessionInvalidError(
        "Unsupported authentication mode.",
        code="invalid_auth_mode",
        status_code=503,
    )


async def _wait_for_running_service_purge(
    user_id: str,
    provider: str,
    service: str,
) -> None:
    """Ensure any in-flight local 2A purge for the same service finishes before saving new tokens (§4.2(3))."""
    running_ids = await db.has_running_local_purge_for_service(
        user_id, provider, service
    )
    for pid in running_ids:
        await finish_disconnect_locked(pid, revoker=None)


async def _stage_pending_confirmation(
    *,
    confirmation_hash: str,
    user_id: str,
    provider: str,
    service: str,
    oauth_client_id: str,
    provider_account_id: str,
    provider_union_id: str | None,
    provider_email: str | None,
    granted_scopes: str,
    access_token: str | None,
    refresh_token: str | None,
    expires_at: datetime | None,
    intent: str,
    preferences: dict[str, Any] | None,
    confirm_expires_at: datetime,
    expected_old_oauth_client_id: str | None = None,
    expected_old_provider_account_id: str | None = None,
    expected_old_auth_epoch: int | None = None,
    _revoke_lock_held: bool = False,
) -> None:
    """Stage a pending confirmation coordinated with Google's ``(project, sub)`` revoke lock (§4.2(3))."""
    if provider == "google":
        google_project = extract_google_project(oauth_client_id)

        async def _commit_stage_tx() -> None:
            async with get_db_connection() as tx_conn, tx_conn.transaction():
                await db.supersede_active_revokes_for_google_project_sub_tx(
                    user_id,
                    google_project,
                    provider_account_id,
                    reason="superseded_by_pending_authorization",
                    conn=tx_conn,
                )
                await db.insert_pending_confirmation(
                    confirmation_hash=confirmation_hash,
                    user_id=user_id,
                    provider=provider,
                    service=service,
                    oauth_client_id=oauth_client_id,
                    provider_account_id=provider_account_id,
                    provider_union_id=provider_union_id,
                    provider_email=provider_email,
                    granted_scopes=granted_scopes,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expires_at=expires_at,
                    intent=intent,
                    preferences=preferences,
                    confirm_expires_at=confirm_expires_at,
                    expected_old_oauth_client_id=expected_old_oauth_client_id,
                    expected_old_provider_account_id=expected_old_provider_account_id,
                    expected_old_auth_epoch=expected_old_auth_epoch,
                    conn=tx_conn,
                )

        if _revoke_lock_held:
            await _commit_stage_tx()
            return

        rkey = revoke_lock_key(user_id, google_project, provider_account_id)
        async with get_db_connection() as lock_conn:
            await lock_conn.execute("SELECT pg_advisory_lock(%s)", (rkey,))
            try:
                await _commit_stage_tx()
            finally:
                with anyio.CancelScope(shield=True):
                    await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (rkey,))
        return

    await db.insert_pending_confirmation(
        confirmation_hash=confirmation_hash,
        user_id=user_id,
        provider=provider,
        service=service,
        oauth_client_id=oauth_client_id,
        provider_account_id=provider_account_id,
        provider_union_id=provider_union_id,
        provider_email=provider_email,
        granted_scopes=granted_scopes,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        intent=intent,
        preferences=preferences,
        confirm_expires_at=confirm_expires_at,
        expected_old_oauth_client_id=expected_old_oauth_client_id,
        expected_old_provider_account_id=expected_old_provider_account_id,
        expected_old_auth_epoch=expected_old_auth_epoch,
    )


async def _finalize_connection(
    *,
    user_id: str,
    provider: str,
    service: str,
    oauth_client_id: str,
    provider_account_id: str,
    provider_union_id: str | None,
    provider_email: str | None,
    granted_scopes: str,
    access_token: str,
    refresh_token: str | None,
    expires_at: datetime | None,
    preferences: dict[str, Any],
    allow_account_switch: bool,
    revoker: RemoteRevoker | None = None,
    expected_old_oauth_client_id: str | None = None,
    expected_old_provider_account_id: str | None = None,
    expected_old_auth_epoch: int | None = None,
    guard_confirmation_hash: str | None = None,
) -> tuple[IntegrationRow, str | None]:
    """Commit a verified OAuth grant into ``dalton_user_integrations``.

    Handles all 3 Google cases (first connect, same-sub client rotation rebind preserving
    history, confirmed account switch reusing Step 1 Phase 1 + 2A purge) and Feishu connect.
    Coordinates with ``revoke_lock_key(user_id, google_project, sub)`` inside the commit
    transaction (strict unidirectional order: lifecycle -> refresh -> revoke) so pending
    revocations are marked ``superseded`` BEFORE the new grant commits.
    When ``expected_old_*`` are bound (from a confirmation ticket), verifies them inside
    the ``FOR UPDATE`` transaction across EVERY commit branch (both 2A switch and normal
    upsert/rebind) so stale confirmation tickets never overwrite or resurrect a changed row.
    Returns ``(connection_row, purge_id_if_account_switch)``.
    """
    await _wait_for_running_service_purge(user_id, provider, service)

    has_expected_old = (
        expected_old_oauth_client_id is not None
        or expected_old_provider_account_id is not None
        or expected_old_auth_epoch is not None
    )

    # Check if account switch is required BEFORE holding lifecycle lock because
    # Step 1's replace_connection_for_account_switch acquires lifecycle_lock_key itself.
    pre_existing = await db.get_connection(user_id, provider, service)
    if (
        pre_existing is not None
        and pre_existing.provider_account_id != provider_account_id
    ):
        if not allow_account_switch:
            raise PersonalIntegrationError(
                "Switching to a different external account requires explicit user confirmation.",
                code="account_switch_confirmation_required",
                status_code=409,
            )
        if not refresh_token:
            raise MissingRefreshTokenOnAccountSwitchError(
                "missing_refresh_token_on_account_switch"
            )
        try:
            switch_res = await replace_connection_for_account_switch(
                user_id,
                provider,
                service,
                new_oauth_client_id=oauth_client_id,
                new_provider_account_id=provider_account_id,
                new_granted_scopes=granted_scopes,
                new_access_token=access_token,
                new_refresh_token=refresh_token,
                new_provider_email=provider_email,
                new_provider_union_id=provider_union_id,
                new_expires_at=expires_at,
                new_preferences=preferences,
                expected_old_oauth_client_id=expected_old_oauth_client_id,
                expected_old_provider_account_id=expected_old_provider_account_id,
                expected_old_auth_epoch=expected_old_auth_epoch,
            )
        except (AccountSwitchTargetChangedError, IntegrationNotFoundError):
            if has_expected_old:
                raise PendingConfirmationInvalidError(
                    "Connected account changed before confirmation; confirmation ticket invalidated.",
                    code="account_switch_state_changed",
                    status_code=409,
                ) from None
            raise
        eff_revoker = (
            revoker if revoker is not None else (get_google_remote_revoker() if provider == "google" else None)
        )
        await finish_disconnect(switch_res.purge_id, revoker=eff_revoker)
        updated = await db.get_connection(user_id, provider, service)
        assert updated is not None
        return updated, switch_res.purge_id

    # Same account (first connect, same-client reconnect/incremental, or client rotation rebind)
    enc_key = get_encryption_key()
    life_key = lifecycle_lock_key(user_id, provider)
    revoke_key = (
        revoke_lock_key(
            user_id,
            extract_google_project(oauth_client_id),
            provider_account_id,
        )
        if provider == "google"
        else None
    )
    cross_project_revoke_purge_id: str | None = None

    async with get_db_connection() as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock(%s)", (life_key,))
        refresh_keys: list[int] = []
        revoke_locked = False
        try:
            # Determine refresh lock key(s) in deterministic ascending order (unidirectional: lifecycle -> refresh -> revoke)
            r_keys_set: set[int] = set()
            r_keys_set.add(
                refresh_lock_key(
                    user_id,
                    provider,
                    oauth_client_id,
                    provider_account_id,
                    service if provider == "feishu" else None,
                )
            )
            if (
                pre_existing is not None
                and pre_existing.oauth_client_id != oauth_client_id
            ):
                r_keys_set.add(
                    refresh_lock_key(
                        user_id,
                        provider,
                        pre_existing.oauth_client_id,
                        pre_existing.provider_account_id,
                        service if provider == "feishu" else None,
                    )
                )
            refresh_keys = sorted(r_keys_set)
            for rk in refresh_keys:
                await lock_conn.execute("SELECT pg_advisory_lock(%s)", (rk,))

            if revoke_key is not None:
                await lock_conn.execute("SELECT pg_advisory_lock(%s)", (revoke_key,))
                revoke_locked = True

            async with get_db_connection() as tx_conn, tx_conn.transaction():
                # Supersede pending revokes for this (Google project, sub) BEFORE committing the new grant
                if provider == "google":
                    await db.supersede_active_revokes_for_google_project_sub_tx(
                        user_id,
                        extract_google_project(oauth_client_id),
                        provider_account_id,
                        conn=tx_conn,
                    )

                existing = await db.get_connection(
                    user_id,
                    provider,
                    service,
                    decrypt_tokens=True,
                    for_update=True,
                    conn=tx_conn,
                )
                # Re-verify confirmation ticket's bound old connection INSIDE the FOR UPDATE
                # lifecycle transaction across ALL commit branches (normal connect/reauth/rebind
                # as well as 2A account switch). Missing old row or changed client/account/epoch
                # invalidates the stale confirmation ticket without mutating DB state.
                if has_expected_old:
                    old_matches = (
                        existing is not None
                        and (
                            expected_old_oauth_client_id is None
                            or existing.oauth_client_id == expected_old_oauth_client_id
                        )
                        and (
                            expected_old_provider_account_id is None
                            or existing.provider_account_id == expected_old_provider_account_id
                        )
                        and (
                            expected_old_auth_epoch is None
                            or existing.auth_epoch == int(expected_old_auth_epoch)
                        )
                    )
                    if not old_matches:
                        raise PendingConfirmationInvalidError(
                            "Connected account changed before confirmation; confirmation ticket invalidated.",
                            code="account_switch_state_changed",
                            status_code=409,
                        )
                # Guard against a concurrent switch changing provider_account_id between pre-check and lock
                if (
                    existing is not None
                    and existing.provider_account_id != provider_account_id
                ):
                    raise PersonalIntegrationError(
                        "Connected account changed concurrently; please retry.",
                        code="account_switch_confirmation_required",
                        status_code=409,
                    )

                new_client_siblings = await db.list_connections_by_account(
                    user_id,
                    provider,
                    oauth_client_id,
                    provider_account_id,
                    decrypt_tokens=True,
                    for_update=True,
                    conn=tx_conn,
                )
                active_new_siblings = [
                    r
                    for r in new_client_siblings
                    if r.service != service
                    and r.status
                    not in (
                        IntegrationStatus.DISCONNECT_PENDING,
                        IntegrationStatus.REVOKED_PURGING,
                    )
                ]

                effective_refresh = refresh_token
                if not effective_refresh:
                    if existing is not None and existing.oauth_client_id == oauth_client_id and existing.refresh_token:
                        effective_refresh = existing.refresh_token
                    else:
                        sib_with_refresh = next(
                            (r for r in active_new_siblings if r.refresh_token),
                            None,
                        )
                        if sib_with_refresh is not None:
                            effective_refresh = sib_with_refresh.refresh_token

                is_client_rotation = (
                    existing is not None
                    and existing.oauth_client_id != oauth_client_id
                )

                if is_client_rotation and not effective_refresh:
                    raise MissingRefreshTokenOnClientRotationError(
                        "Reconnecting after OAuth client rotation requires a refresh token.",
                    )
                if existing is None and not effective_refresh:
                    raise MissingRefreshTokenError(
                        "OAuth provider did not return a refresh token; please re-authorize with consent.",
                    )

                if is_client_rotation:
                    assert existing is not None
                    old_client_id = existing.oauth_client_id
                    old_auth_epoch = existing.auth_epoch

                    # Read raw encrypted old refresh/access token bytes before overwriting
                    async with tx_conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(
                            """
                            SELECT refresh_token, access_token
                            FROM dalton_user_integrations
                            WHERE user_id = %s AND provider = %s AND service = %s
                            """,
                            (user_id, provider, service),
                        )
                        raw_old = await cur.fetchone()
                    raw_old_bytes = (
                        bytes(raw_old["refresh_token"])
                        if raw_old and raw_old.get("refresh_token") is not None
                        else (
                            bytes(raw_old["access_token"])
                            if raw_old and raw_old.get("access_token") is not None
                            else None
                        )
                    )

                    # Rebind IN PLACE preserving conversation history (local_purge_status = 'not_required')
                    merged_prefs = dict(existing.preferences)
                    merged_prefs.update(preferences)
                    saved_row = await db.upsert_connection(
                        user_id=user_id,
                        provider=provider,
                        service=service,
                        oauth_client_id=oauth_client_id,
                        provider_account_id=provider_account_id,
                        provider_union_id=provider_union_id,
                        provider_email=provider_email,
                        granted_scopes=granted_scopes,
                        access_token=access_token,
                        refresh_token=effective_refresh,
                        expires_at=expires_at,
                        preferences=merged_prefs,
                        status=IntegrationStatus.CONNECTED,
                        bump_auth_epoch=True,
                        reset_authorized_at=True,
                        reset_token_generation=True,
                        conn=tx_conn,
                    )

                    if provider == "google":
                        remaining_on_old = await db.get_sibling_connections(
                            user_id,
                            "google",
                            old_client_id,
                            provider_account_id,
                            exclude_service=service,
                            active_only=True,
                            conn=tx_conn,
                        )
                        if remaining_on_old:
                            r_status = RemoteRevokeStatus.NOT_APPLICABLE
                            r_tok = None
                        elif extract_google_project(old_client_id) == extract_google_project(
                            oauth_client_id
                        ):
                            # Same Google Cloud project: new token already issued -> mark superseded
                            r_status = RemoteRevokeStatus.SUPERSEDED
                            r_tok = None
                        elif raw_old_bytes is not None:
                            r_status = RemoteRevokeStatus.PENDING
                            r_tok = raw_old_bytes
                        else:
                            r_status = RemoteRevokeStatus.NOT_APPLICABLE
                            r_tok = None

                        ledger_row = await db.insert_purge_ledger(
                            purge_id=str(uuid4()),
                            user_id=user_id,
                            provider="google",
                            service=service,
                            oauth_client_id=old_client_id,
                            provider_account_id=provider_account_id,
                            auth_epoch_upto=old_auth_epoch,
                            trigger_reason=TriggerReason.CLIENT_ROTATION,
                            local_purge_status=LocalPurgeStatus.NOT_REQUIRED,
                            purged_max_turn_by_thread={},
                            remote_revoke_status=r_status,
                            encrypted_revoke_token=r_tok,
                            conn=tx_conn,
                        )
                        if r_status == RemoteRevokeStatus.PENDING:
                            cross_project_revoke_purge_id = ledger_row.purge_id
                else:
                    # First connect OR same-account same-client reconnect/incremental auth
                    # Always refresh authorized_at on explicit OAuth activation (and bump auth_epoch for Feishu reauth)
                    merged_prefs = dict(existing.preferences) if existing is not None else {}
                    merged_prefs.update(preferences)
                    need_new_epoch = (
                        existing is None
                        or existing.status != IntegrationStatus.CONNECTED
                        or provider == "feishu"
                    )
                    saved_row = await db.upsert_connection(
                        user_id=user_id,
                        provider=provider,
                        service=service,
                        oauth_client_id=oauth_client_id,
                        provider_account_id=provider_account_id,
                        provider_union_id=provider_union_id,
                        provider_email=provider_email,
                        granted_scopes=granted_scopes,
                        access_token=access_token,
                        refresh_token=effective_refresh,
                        expires_at=expires_at,
                        preferences=merged_prefs,
                        status=IntegrationStatus.CONNECTED,
                        bump_auth_epoch=need_new_epoch,
                        reset_authorized_at=True,
                        reset_token_generation=(existing is None),
                        conn=tx_conn,
                    )

                # For Google: update active sibling rows on same (oauth_client_id, sub) with
                # the NEW token's ACTUAL granted scopes (never union with stale sibling scopes)
                # and recompute each sibling's status & feature preferences against its own
                # minimum required scopes. Siblings preserve their own auth_epoch and authorized_at.
                if provider == "google" and effective_refresh and active_new_siblings:
                    actual_granted_list = _parse_scopes(granted_scopes)
                    actual_granted_str = _format_scopes(actual_granted_list)
                    async with tx_conn.cursor() as cur:
                        for sib in active_new_siblings:
                            sib_prefs, _ = _converge_preferences_for_scopes(
                                "google",
                                sib.service,
                                actual_granted_list,
                                sib.preferences,
                            )
                            sib_missing = _missing_min_scopes(
                                "google",
                                sib.service,
                                actual_granted_list,
                            )
                            if sib_missing:
                                sib_status = IntegrationStatus.NEEDS_REAUTH.value
                                sib_err_code: str | None = "missing_required_scopes"
                            else:
                                sib_status = IntegrationStatus.CONNECTED.value
                                sib_err_code = None
                            await cur.execute(
                                """
                                UPDATE dalton_user_integrations
                                SET access_token = pgp_sym_encrypt(%(access_token)s, %(enc_key)s),
                                    refresh_token = pgp_sym_encrypt(%(refresh_token)s, %(enc_key)s),
                                    expires_at = %(expires_at)s,
                                    granted_scopes = %(granted_scopes)s,
                                    preferences = %(preferences)s,
                                    token_generation = token_generation + 1,
                                    refresh_inflight_gen = NULL,
                                    refresh_started_at = NULL,
                                    status = %(status)s,
                                    needs_reauth_at = CASE WHEN %(status)s = 'needs_reauth'
                                                           THEN COALESCE(needs_reauth_at, NOW())
                                                           ELSE NULL END,
                                    last_error_code = %(err_code)s,
                                    updated_at = NOW()
                                WHERE user_id = %(user_id)s
                                  AND provider = 'google'
                                  AND service = %(service)s
                                  AND oauth_client_id = %(oauth_client_id)s
                                  AND provider_account_id = %(provider_account_id)s
                                """,
                                {
                                    "access_token": access_token,
                                    "refresh_token": effective_refresh,
                                    "enc_key": enc_key,
                                    "expires_at": expires_at,
                                    "granted_scopes": actual_granted_str,
                                    "preferences": Jsonb(sib_prefs),
                                    "status": sib_status,
                                    "err_code": sib_err_code,
                                    "user_id": user_id,
                                    "service": sib.service,
                                    "oauth_client_id": oauth_client_id,
                                    "provider_account_id": provider_account_id,
                                },
                            )

                if guard_confirmation_hash is not None:
                    await db.delete_pending_confirmation_by_hash(
                        guard_confirmation_hash,
                        user_id,
                        conn=tx_conn,
                    )
        finally:
            with anyio.CancelScope(shield=True):
                if revoke_locked and revoke_key is not None:
                    await lock_conn.execute(
                        "SELECT pg_advisory_unlock(%s)", (revoke_key,)
                    )
                for rk in reversed(refresh_keys):
                    await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (rk,))
                await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (life_key,))

    if provider == "google" and cross_project_revoke_purge_id is not None and revoker is not None:
        from src.dalton.services.integration_revoke import process_revoke

        try:
            await process_revoke(cross_project_revoke_purge_id, revoker)
        except Exception as exc:
            logger.warning(
                "[integration-oauth] cross-project rotation revoke deferred (%s)",
                type(exc).__name__,
            )

    return saved_row, None


async def handle_oauth_callback(
    *,
    provider: str,
    code: str | None,
    state: str | None,
    error: str | None = None,
    error_description: str | None = None,
    browser_binding_token: str | None = None,
    google_client: GoogleOAuthClientProtocol | None = None,
    feishu_client: FeishuOAuthClientProtocol | None = None,
    revoker: RemoteRevoker | None = None,
) -> OAuthCallbackResult:
    """Handle the Bearer-less OAuth browser callback (§4.2(1), §4.3, §6).

    1. For Google, acquires the shared ``(user_id, google_project, sub)`` revoke advisory lock
       BEFORE consuming state and exchanging ``code``, keeping the lock continuously until
       pending confirmation or commit-guard + ``superseded`` transition commits.
    2. Atomically consumes ``state`` in PostgreSQL (single-use, 10-minute TTL).
    3. Checks per-service capability, active ``oauth_client_id``, and original session.
    4. Exchanges ``code`` outside any DB transaction and verifies external identity.
    5. Stages account-switch, Supabase, or mismatched-browser ``local`` confirmations,
       or commits immediately under canonical ``lifecycle -> refresh -> revoke`` order.
    """
    _ = error_description  # never echo arbitrary upstream description text
    norm_provider = str(provider or "").strip().lower()
    if norm_provider not in ("google", "feishu"):
        raise OAuthStateInvalidError("Unsupported provider.", code="unsupported_provider")

    if not state or not str(state).strip():
        raise OAuthStateInvalidError("Missing OAuth state.", code="invalid_or_expired_state")

    state_hash = _sha256_hex(str(state).strip())

    # For Google: peek state and acquire candidate (user_id, google_project, sub) revoke_locks
    # in deterministic ascending order BEFORE consuming state or calling exchange_code, so no
    # background revoke can race between user consent / code exchange and DB staging/commit.
    revoke_lock_cm: Any = None
    revoke_lock_conn: Any = None
    held_revoke_keys: list[int] = []

    async def _release_all_revoke_locks() -> None:
        nonlocal revoke_lock_cm, revoke_lock_conn
        if revoke_lock_conn is None:
            held_revoke_keys.clear()
            return
        with anyio.CancelScope(shield=True):
            for rk in reversed(held_revoke_keys):
                await revoke_lock_conn.execute("SELECT pg_advisory_unlock(%s)", (rk,))
            held_revoke_keys.clear()
            cm = revoke_lock_cm
            revoke_lock_cm = None
            revoke_lock_conn = None
            if cm is not None:
                await cm.__aexit__(None, None, None)

    async def _ensure_single_revoke_lock(target_key: int) -> None:
        nonlocal revoke_lock_cm, revoke_lock_conn
        if target_key in held_revoke_keys:
            for rk in [k for k in reversed(held_revoke_keys) if k != target_key]:
                await revoke_lock_conn.execute("SELECT pg_advisory_unlock(%s)", (rk,))
                held_revoke_keys.remove(rk)
            return
        # Release any non-matching candidate locks first so lock acquisition never inverts order
        if held_revoke_keys and revoke_lock_conn is not None:
            for rk in reversed(held_revoke_keys):
                await revoke_lock_conn.execute("SELECT pg_advisory_unlock(%s)", (rk,))
            held_revoke_keys.clear()
        if revoke_lock_conn is None:
            revoke_lock_cm = get_db_connection()
            revoke_lock_conn = await revoke_lock_cm.__aenter__()
        await revoke_lock_conn.execute("SELECT pg_advisory_lock(%s)", (target_key,))
        held_revoke_keys.append(target_key)

    guard_confirmation_hash: str | None = None
    guard_user_id: str | None = None
    try:
        if norm_provider == "google":
            peeked = await db.peek_oauth_state(state_hash, norm_provider)
            if peeked is not None:
                pre_uid, pre_cid = peeked
                pre_proj = extract_google_project(pre_cid)
                cand_subs = await db.list_google_project_revoke_lock_subs(
                    pre_uid, pre_proj
                )
                if cand_subs:
                    cand_keys = sorted(
                        {revoke_lock_key(pre_uid, pre_proj, s) for s in cand_subs}
                    )
                    revoke_lock_cm = get_db_connection()
                    revoke_lock_conn = await revoke_lock_cm.__aenter__()
                    for rk in cand_keys:
                        await revoke_lock_conn.execute(
                            "SELECT pg_advisory_lock(%s)", (rk,)
                        )
                        held_revoke_keys.append(rk)

        state_row = await db.consume_oauth_state(state_hash, norm_provider)
        if state_row is None:
            raise OAuthStateInvalidError(
                "OAuth state is invalid, expired, or already used.",
                code="invalid_or_expired_state",
            )

        safe_return_to = _sanitize_return_to(state_row.return_to)

        # Check per-service single capability & readiness (§6 item 1)
        require_personal_integration_capability(
            state_row.provider, state_row.service, mode="connect"
        )

        if error:
            safe_err = "".join(
                ch for ch in str(error).strip().lower()[:64] if ch.isalnum() or ch in ("_", "-")
            ) or "oauth_denied"
            return OAuthCallbackResult(
                status="error",
                provider=state_row.provider,
                service=state_row.service,
                return_to=safe_return_to,
                error_code=safe_err,
                error_message="Authorization was cancelled or denied by the provider.",
            )

        if not code or not str(code).strip():
            return OAuthCallbackResult(
                status="error",
                provider=state_row.provider,
                service=state_row.service,
                return_to=safe_return_to,
                error_code="missing_authorization_code",
                error_message="Authorization code is missing.",
            )

        client_id, client_secret = _load_provider_oauth_credentials(norm_provider)
        if client_id != state_row.oauth_client_id:
            return OAuthCallbackResult(
                status="error",
                provider=state_row.provider,
                service=state_row.service,
                return_to=safe_return_to,
                error_code="oauth_client_changed",
                error_message="Deployment OAuth client changed while authorizing; please retry.",
            )

        # Verify original session per auth_mode (§4.2(1) item 2)
        await _verify_original_session_for_callback(
            auth_mode=state_row.auth_mode,
            user_id=state_row.user_id,
            session_id=state_row.session_id,
            user_email=state_row.user_email,
        )

        # Upstream code -> token exchange OUTSIDE any DB transaction (under Google revoke_lock)
        provider_union_id: str | None = None
        provider_email: str | None = None
        try:
            if norm_provider == "google":
                g_client = google_client or get_google_oauth_client()
                token_resp = await g_client.exchange_code(
                    client_id=client_id,
                    client_secret=client_secret,
                    code=str(code).strip(),
                    redirect_uri=state_row.redirect_uri,
                    code_verifier=state_row.code_verifier or "",
                )
                if not token_resp.id_token:
                    return OAuthCallbackResult(
                        status="error",
                        provider=norm_provider,
                        service=state_row.service,
                        return_to=safe_return_to,
                        error_code="missing_id_token",
                        error_message="Google did not return an OIDC id_token.",
                    )
                claims = await g_client.verify_id_token(
                    id_token=token_resp.id_token,
                    expected_aud=client_id,
                )
                provider_account_id = claims.sub
                provider_email = claims.email
                granted_list = _parse_scopes(token_resp.scope)
            else:
                f_client = feishu_client or get_feishu_oauth_client()
                token_resp = await f_client.exchange_code(
                    app_id=client_id,
                    app_secret=client_secret,
                    code=str(code).strip(),
                    redirect_uri=state_row.redirect_uri,
                    code_verifier=state_row.code_verifier or "",
                )
                user_info = await f_client.get_user_info(
                    access_token=token_resp.access_token
                )
                provider_account_id = user_info.open_id
                provider_union_id = user_info.union_id
                provider_email = user_info.email
                granted_list = _parse_scopes(token_resp.scope or state_row.requested_scopes)

                # Channel gateway Feishu identity consistency check (§4.3 item 2):
                # Compare open_id against dalton_channel_identities ONLY when the OAuth app
                # shares the exact same Feishu App ID as the active channel gateway (resolved
                # from the exact same deployment credentials source the gateway uses).
                gateway_app_id = await _resolve_channel_gateway_feishu_app_id()
                if gateway_app_id and gateway_app_id == client_id:
                    bound_open_id = await db.get_channel_feishu_open_id(state_row.user_id)
                    if bound_open_id and bound_open_id != provider_account_id:
                        raise FeishuIdentityMismatchError(
                            "Feishu OAuth account does not match your bound Feishu channel identity."
                        )
        except (OAuthUpstreamError, GoogleIdTokenVerificationError) as exc:
            err_code = (
                exc.error_code
                if isinstance(exc, OAuthUpstreamError)
                else exc.reason
            )
            return OAuthCallbackResult(
                status="error",
                provider=norm_provider,
                service=state_row.service,
                return_to=safe_return_to,
                error_code=err_code,
                error_message="Failed to complete OAuth token exchange with provider.",
            )

        # Validate minimum required scopes (§3.3, §4.2(1))
        missing_scopes = _missing_min_scopes(norm_provider, state_row.service, granted_list)
        if missing_scopes:
            return OAuthCallbackResult(
                status="error",
                provider=norm_provider,
                service=state_row.service,
                return_to=safe_return_to,
                error_code="missing_required_scopes",
                error_message=(
                    "授权范围不完整（缺少必需权限），请重新连接并勾选全部基础权限。"
                ),
            )

        requested_list = _parse_scopes(state_row.requested_scopes)
        service_prefs, partial_grant = _converge_preferences_for_scopes(
            norm_provider,
            state_row.service,
            granted_list,
            {},
            requested_scopes=requested_list,
        )
        granted_scopes_str = _format_scopes(granted_list)

        # Check if this callback selects a DIFFERENT external account than the existing connection
        existing = await db.get_connection(
            state_row.user_id, norm_provider, state_row.service
        )
        is_account_switch = (
            existing is not None and existing.provider_account_id != provider_account_id
        )

        # Narrow or acquire the exact (user_id, google_project, sub) revoke_lock before staging/guarding
        if norm_provider == "google":
            target_google_project = extract_google_project(client_id)
            target_rkey = revoke_lock_key(
                state_row.user_id, target_google_project, provider_account_id
            )
            await _ensure_single_revoke_lock(target_rkey)

        if is_account_switch:
            if not token_resp.refresh_token:
                return OAuthCallbackResult(
                    status="error",
                    provider=norm_provider,
                    service=state_row.service,
                    return_to=safe_return_to,
                    error_code="missing_refresh_token_on_account_switch",
                    error_message="切换账号时未获得新的刷新令牌，已保留原账号连接不变。",
                )
            raw_confirm = secrets.token_urlsafe(32)
            confirm_hash = _sha256_hex(raw_confirm)
            confirm_expires = datetime.now(UTC) + timedelta(
                seconds=PENDING_CONFIRMATION_TTL_SECONDS
            )
            staged_prefs = {
                **service_prefs,
                "_account_switch_previous_email": existing.provider_email,
                "_account_switch_previous_account_id": existing.provider_account_id,
            }
            await _stage_pending_confirmation(
                confirmation_hash=confirm_hash,
                user_id=state_row.user_id,
                provider=norm_provider,
                service=state_row.service,
                oauth_client_id=client_id,
                provider_account_id=provider_account_id,
                provider_union_id=provider_union_id,
                provider_email=provider_email,
                granted_scopes=granted_scopes_str,
                access_token=token_resp.access_token,
                refresh_token=token_resp.refresh_token,
                expires_at=token_resp.expires_at,
                intent=OAuthIntent.SWITCH_ACCOUNT.value,
                preferences=staged_prefs,
                confirm_expires_at=confirm_expires,
                expected_old_oauth_client_id=existing.oauth_client_id,
                expected_old_provider_account_id=existing.provider_account_id,
                expected_old_auth_epoch=existing.auth_epoch,
                _revoke_lock_held=(norm_provider == "google"),
            )
            return OAuthCallbackResult(
                status="account_switch_confirmation_required",
                provider=norm_provider,
                service=state_row.service,
                return_to=safe_return_to,
                confirmation_token=raw_confirm,
                previous_account_email=existing.provider_email,
                new_account_email=provider_email,
                partial_grant=partial_grant,
            )

        # Determine whether post-callback authenticated confirmation is required:
        # - Supabase mode always stages tokens for 5-minute authenticated Bearer confirmation.
        # - Local mode verifies the one-time browser-binding cookie; if missing or mismatched
        #   (e.g., another browser finished the OAuth flow), stage for logged-in page confirmation.
        local_browser_mismatch = False
        if state_row.auth_mode == "local":
            cleaned_cookie = (
                browser_binding_token.strip()
                if isinstance(browser_binding_token, str) and browser_binding_token.strip()
                else ""
            )
            expected_hash = state_row.browser_binding_hash or ""
            if not expected_hash or not cleaned_cookie or not hmac.compare_digest(
                _sha256_hex(cleaned_cookie), expected_hash
            ):
                local_browser_mismatch = True

        if state_row.auth_mode == "supabase" or local_browser_mismatch:
            raw_confirm = secrets.token_urlsafe(32)
            confirm_hash = _sha256_hex(raw_confirm)
            confirm_expires = datetime.now(UTC) + timedelta(
                seconds=PENDING_CONFIRMATION_TTL_SECONDS
            )
            await _stage_pending_confirmation(
                confirmation_hash=confirm_hash,
                user_id=state_row.user_id,
                provider=norm_provider,
                service=state_row.service,
                oauth_client_id=client_id,
                provider_account_id=provider_account_id,
                provider_union_id=provider_union_id,
                provider_email=provider_email,
                granted_scopes=granted_scopes_str,
                access_token=token_resp.access_token,
                refresh_token=token_resp.refresh_token,
                expires_at=token_resp.expires_at,
                intent=state_row.intent,
                preferences=service_prefs,
                confirm_expires_at=confirm_expires,
                expected_old_oauth_client_id=(
                    existing.oauth_client_id if existing is not None else None
                ),
                expected_old_provider_account_id=(
                    existing.provider_account_id if existing is not None else None
                ),
                expected_old_auth_epoch=(
                    existing.auth_epoch if existing is not None else None
                ),
                _revoke_lock_held=(norm_provider == "google"),
            )
            return OAuthCallbackResult(
                status="confirmation_required",
                provider=norm_provider,
                service=state_row.service,
                return_to=safe_return_to,
                confirmation_token=raw_confirm,
                new_account_email=provider_email,
                partial_grant=partial_grant,
            )

        # Direct commit (local same-browser / oss):
        # For Google, BEFORE releasing revoke_lock, supersede all active revokes for
        # (user_id, google_project, sub) AND insert a short-lived tokenless commit-guard row
        # into pending_confirmations in ONE transaction so no concurrent revoke can sneak in
        # while we release revoke_lock and re-enter _finalize_connection in canonical
        # lifecycle -> refresh -> revoke order.
        if norm_provider == "google":
            guard_confirmation_hash = _sha256_hex(
                f"commit_guard:{secrets.token_urlsafe(32)}"
            )
            guard_user_id = state_row.user_id
            await _stage_pending_confirmation(
                confirmation_hash=guard_confirmation_hash,
                user_id=state_row.user_id,
                provider="google",
                service=state_row.service,
                oauth_client_id=client_id,
                provider_account_id=provider_account_id,
                provider_union_id=provider_union_id,
                provider_email=provider_email,
                granted_scopes=granted_scopes_str,
                access_token=None,
                refresh_token=None,
                expires_at=token_resp.expires_at,
                intent=state_row.intent,
                preferences={"_internal_commit_guard": True},
                confirm_expires_at=datetime.now(UTC) + timedelta(seconds=60),
                _revoke_lock_held=True,
            )
    finally:
        await _release_all_revoke_locks()

    # local (same initiating browser) / oss mode (first connect, same-account reconnect, or same-sub client rotation)
    eff_revoker = (
        revoker if revoker is not None else (get_google_remote_revoker() if norm_provider == "google" else None)
    )
    try:
        saved_row, _ = await _finalize_connection(
            user_id=state_row.user_id,
            provider=norm_provider,
            service=state_row.service,
            oauth_client_id=client_id,
            provider_account_id=provider_account_id,
            provider_union_id=provider_union_id,
            provider_email=provider_email,
            granted_scopes=granted_scopes_str,
            access_token=token_resp.access_token,
            refresh_token=token_resp.refresh_token,
            expires_at=token_resp.expires_at,
            preferences=service_prefs,
            allow_account_switch=False,
            revoker=eff_revoker,
            guard_confirmation_hash=guard_confirmation_hash,
        )
    except (
        MissingRefreshTokenOnClientRotationError,
        MissingRefreshTokenError,
        MissingRefreshTokenOnAccountSwitchError,
    ) as exc:
        if guard_confirmation_hash is not None and guard_user_id is not None:
            with anyio.CancelScope(shield=True):
                await db.delete_pending_confirmation_by_hash(
                    guard_confirmation_hash, guard_user_id
                )
        return OAuthCallbackResult(
            status="error",
            provider=norm_provider,
            service=state_row.service,
            return_to=safe_return_to,
            error_code=getattr(exc, "code", "missing_refresh_token"),
            error_message=str(exc),
        )
    except BaseException:
        if guard_confirmation_hash is not None and guard_user_id is not None:
            with anyio.CancelScope(shield=True):
                await db.delete_pending_confirmation_by_hash(
                    guard_confirmation_hash, guard_user_id
                )
        raise

    return OAuthCallbackResult(
        status="connected",
        provider=norm_provider,
        service=state_row.service,
        return_to=safe_return_to,
        connection=saved_row,
        new_account_email=provider_email,
        partial_grant=partial_grant,
    )


async def confirm_pending_integration(
    *,
    user_id: str,
    confirmation_token: str,
    confirm_account_switch: bool = False,
    cancel: bool = False,
    revoker: RemoteRevoker | None = None,
) -> ConfirmIntegrationResult:
    """Confirm (or cancel) a staged Supabase, mismatched-browser local, or account-switch OAuth connection (§4.2(1))."""
    cleaned = str(confirmation_token or "").strip()
    if not cleaned:
        raise PendingConfirmationInvalidError(
            "Confirmation token is missing.",
            code="invalid_or_expired_confirmation",
        )
    confirm_hash = _sha256_hex(cleaned)

    if cancel:
        consumed = await db.consume_pending_confirmation(confirm_hash, user_id)
        if consumed is None:
            raise PendingConfirmationInvalidError(
                "Confirmation token is invalid, expired, or belongs to another user.",
            )
        return ConfirmIntegrationResult(
            status="cancelled",
            provider=consumed.provider,
            service=consumed.service,
        )

    # Peek first without consuming so an unconfirmed account-switch can return 409 safely
    preview = await db.peek_pending_confirmation(confirm_hash, user_id)
    if preview is None:
        raise PendingConfirmationInvalidError(
            "Confirmation token is invalid, expired, or belongs to another user.",
        )

    require_personal_integration_capability(
        preview.provider, preview.service, mode="connect"
    )
    client_id, _secret = _load_provider_oauth_credentials(preview.provider)
    if client_id != preview.oauth_client_id:
        await db.consume_pending_confirmation(confirm_hash, user_id)
        raise PersonalIntegrationError(
            "Deployment OAuth client changed while confirming; please reconnect.",
            code="oauth_client_changed",
            status_code=409,
        )

    existing = await db.get_connection(user_id, preview.provider, preview.service)

    # Verify bound old-connection target (client, account, auth_epoch) for ANY staged ticket
    # carrying old-connection expectations: if the row was deleted or changed since staging,
    # void this ticket immediately before committing.
    if (
        preview.expected_old_oauth_client_id is not None
        or preview.expected_old_provider_account_id is not None
        or preview.expected_old_auth_epoch is not None
    ):
        target_matches = (
            existing is not None
            and (
                preview.expected_old_oauth_client_id is None
                or existing.oauth_client_id == preview.expected_old_oauth_client_id
            )
            and (
                preview.expected_old_provider_account_id is None
                or existing.provider_account_id == preview.expected_old_provider_account_id
            )
            and (
                preview.expected_old_auth_epoch is None
                or existing.auth_epoch == preview.expected_old_auth_epoch
            )
        )
        if not target_matches:
            await db.consume_pending_confirmation(confirm_hash, user_id)
            raise PendingConfirmationInvalidError(
                "Connected account changed since authorization; confirmation ticket has been invalidated.",
                code="account_switch_state_changed",
                status_code=409,
            )

    if (
        existing is not None
        and existing.provider_account_id != preview.provider_account_id
        and not confirm_account_switch
    ):
        return ConfirmIntegrationResult(
            status="account_switch_confirmation_required",
            provider=preview.provider,
            service=preview.service,
            previous_account_email=existing.provider_email,
            new_account_email=preview.provider_email,
        )

    # Atomically consume the pending confirmation owned by user_id
    pending = await db.consume_pending_confirmation(confirm_hash, user_id)
    if pending is None:
        raise PendingConfirmationInvalidError(
            "Confirmation token is invalid, expired, or already used.",
        )

    clean_prefs = {
        k: v for k, v in (pending.preferences or {}).items() if not k.startswith("_")
    }
    eff_revoker = (
        revoker
        if revoker is not None
        else (get_google_remote_revoker() if pending.provider == "google" else None)
    )
    saved_row, purge_id = await _finalize_connection(
        user_id=user_id,
        provider=pending.provider,
        service=pending.service,
        oauth_client_id=pending.oauth_client_id,
        provider_account_id=pending.provider_account_id,
        provider_union_id=pending.provider_union_id,
        provider_email=pending.provider_email,
        granted_scopes=pending.granted_scopes,
        access_token=pending.access_token or "",
        refresh_token=pending.refresh_token,
        expires_at=pending.expires_at,
        preferences=clean_prefs,
        allow_account_switch=confirm_account_switch,
        revoker=eff_revoker,
        expected_old_oauth_client_id=pending.expected_old_oauth_client_id,
        expected_old_provider_account_id=pending.expected_old_provider_account_id,
        expected_old_auth_epoch=pending.expected_old_auth_epoch,
    )
    return ConfirmIntegrationResult(
        status="connected",
        provider=pending.provider,
        service=pending.service,
        connection=saved_row,
        purge_id=purge_id,
        previous_account_email=existing.provider_email if existing is not None else None,
        new_account_email=saved_row.provider_email,
    )


# ---------------------------------------------------------------------------
# 3. Serialized Refresh with CAS & 3-Element Verification (§4.2(2))
# ---------------------------------------------------------------------------


def _remaining_seconds(expires_at: datetime | None, now: datetime) -> float:
    if expires_at is None:
        return 0.0
    exp = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=UTC)
    return (exp - now).total_seconds()


def _check_required_scopes(
    granted_scopes: str, required_scopes: Collection[str] | None
) -> None:
    if not required_scopes:
        return
    granted_set = set(_parse_scopes(granted_scopes))
    missing = [s for s in required_scopes if s not in granted_set]
    if missing:
        raise MissingRequiredScopesError(
            f"Missing required OAuth scopes: {', '.join(missing)}",
            code="missing_required_scopes",
            status_code=403,
            details={"missing_scopes": missing},
        )


def _to_fresh_token(row: IntegrationRow) -> FreshAccessToken:
    assert row.access_token is not None
    return FreshAccessToken(
        access_token=row.access_token,
        provider=row.provider,
        service=row.service,
        oauth_client_id=row.oauth_client_id,
        provider_account_id=row.provider_account_id,
        auth_epoch=row.auth_epoch,
        token_generation=row.token_generation,
        granted_scopes=row.granted_scopes,
        expires_at=row.expires_at,
    )


async def _wait_for_refresh_winner(
    *,
    user_id: str,
    provider: str,
    service: str,
    start_client_id: str,
    start_sub: str,
    start_auth_epoch: int,
    start_token_gen: int,
    min_remaining_seconds: int,
    force_refresh: bool,
    required_scopes: Collection[str] | None,
    budget_seconds: float,
    poll_interval_seconds: float,
) -> FreshAccessToken:
    """Loser/waiter path (§4.2(2) step 1): never calls upstream; polls DB with 3-element verification."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    while True:
        await asyncio.sleep(poll_interval_seconds)
        cur = await db.get_connection(user_id, provider, service, decrypt_tokens=True)
        if cur is None or cur.status in (
            IntegrationStatus.DISCONNECT_PENDING,
            IntegrationStatus.REVOKED_PURGING,
        ):
            raise IntegrationEpochRevokedError("Connection was disconnected or revoked.")
        if (
            cur.oauth_client_id != start_client_id
            or cur.provider_account_id != start_sub
            or cur.auth_epoch != start_auth_epoch
        ):
            raise IntegrationEpochRevokedError(
                "Connection client, account, or auth_epoch changed during refresh."
            )
        if cur.status == IntegrationStatus.NEEDS_REAUTH:
            raise IntegrationNeedsReauthError(
                "Connection requires re-authorization.",
                code=cur.last_error_code or "needs_reauth",
            )
        now = datetime.now(UTC)
        rem = _remaining_seconds(cur.expires_at, now)
        if (
            cur.status == IntegrationStatus.CONNECTED
            and cur.access_token
            and rem >= min_remaining_seconds
            and (not force_refresh or cur.token_generation > start_token_gen)
        ):
            _check_required_scopes(cur.granted_scopes, required_scopes)
            return _to_fresh_token(cur)
        if loop.time() >= deadline:
            raise IntegrationRefreshBusyError(
                "Token refresh is still in progress on another worker; retry shortly."
            )


async def _commit_refresh_success_tx(
    *,
    user_id: str,
    provider: str,
    service: str,
    start_client_id: str,
    start_sub: str,
    start_auth_epoch: int,
    start_token_gen: int,
    token_resp: OAuthTokenResponse,
    required_scopes: Collection[str] | None,
) -> FreshAccessToken:
    """Row-lock CAS commit with 3-element conflict check and Google sibling propagation (§4.2(2) step 3).

    Commits all persisted token/status mutations BEFORE raising business exceptions
    (such as ``IntegrationNeedsReauthError`` on narrowed scopes or ``IntegrationEpochRevokedError``
    after clearing inflight markers on concurrent disconnect) so committed new tokens
    and status transitions are never rolled back.
    """
    enc_key = get_encryption_key()
    committed_row: IntegrationRow | None = None
    deferred_exc: Exception | None = None

    async with get_db_connection() as tx_conn, tx_conn.transaction():
        if provider == "google":
            locked_rows = await db.list_connections_by_account(
                user_id,
                "google",
                start_client_id,
                start_sub,
                decrypt_tokens=True,
                for_update=True,
                conn=tx_conn,
            )
            cur = next((r for r in locked_rows if r.service == service), None)
            if cur is None:
                # Re-fetch by PK in case client/sub changed under concurrent rebind/switch
                cur = await db.get_connection(
                    user_id, provider, service, decrypt_tokens=True, for_update=True, conn=tx_conn
                )
            siblings = [
                r
                for r in locked_rows
                if r.service != service
                and r.status in (IntegrationStatus.CONNECTED, IntegrationStatus.REFRESH_AMBIGUOUS)
            ]
        else:
            cur = await db.get_connection(
                user_id, provider, service, decrypt_tokens=True, for_update=True, conn=tx_conn
            )
            siblings = []

        if cur is None:
            deferred_exc = IntegrationEpochRevokedError("Connection no longer exists.")
        else:
            # 3-element identity check: client, sub, auth_epoch
            identity_matches = (
                cur.oauth_client_id == start_client_id
                and cur.provider_account_id == start_sub
                and cur.auth_epoch == start_auth_epoch
            )
            if not identity_matches:
                deferred_exc = IntegrationEpochRevokedError(
                    "Connection client, account, or auth_epoch changed during refresh."
                )
            elif cur.status in (
                IntegrationStatus.DISCONNECT_PENDING,
                IntegrationStatus.REVOKED_PURGING,
            ):
                # Concurrent disconnect intervened: commit cleanup & Google sibling updates first
                async with tx_conn.cursor() as cur_sql:
                    if provider == "feishu":
                        await cur_sql.execute(
                            """
                            UPDATE dalton_user_integrations
                            SET access_token = NULL,
                                refresh_token = NULL,
                                refresh_inflight_gen = NULL,
                                refresh_started_at = NULL,
                                updated_at = NOW()
                            WHERE user_id = %s AND provider = %s AND service = %s
                            """,
                            (user_id, provider, service),
                        )
                    else:
                        await cur_sql.execute(
                            """
                            UPDATE dalton_user_integrations
                            SET refresh_inflight_gen = NULL,
                                refresh_started_at = NULL,
                                updated_at = NOW()
                            WHERE user_id = %s AND provider = %s AND service = %s
                            """,
                            (user_id, provider, service),
                        )
                        # Google single-service disconnect: still update remaining active siblings
                        for sib in siblings:
                            new_sib_scopes = (
                                _format_scopes(_parse_scopes(token_resp.scope))
                                if token_resp.scope
                                else sib.granted_scopes
                            )
                            sib_scope_list = _parse_scopes(new_sib_scopes)
                            sib_prefs, _ = _converge_preferences_for_scopes(
                                "google",
                                sib.service,
                                sib_scope_list,
                                sib.preferences,
                            )
                            sib_missing = _missing_min_scopes(
                                "google", sib.service, sib_scope_list
                            )
                            sib_status = (
                                IntegrationStatus.NEEDS_REAUTH.value
                                if sib_missing
                                else IntegrationStatus.CONNECTED.value
                            )
                            sib_err_code = "missing_required_scopes" if sib_missing else None
                            await cur_sql.execute(
                                """
                                UPDATE dalton_user_integrations
                                SET access_token = pgp_sym_encrypt(%(access)s, %(enc_key)s),
                                    refresh_token = CASE WHEN %(refresh)s::text IS NULL THEN refresh_token
                                                         ELSE pgp_sym_encrypt(%(refresh)s, %(enc_key)s) END,
                                    expires_at = %(expires_at)s,
                                    granted_scopes = %(scopes)s,
                                    preferences = %(preferences)s,
                                    token_generation = token_generation + 1,
                                    refresh_inflight_gen = NULL,
                                    refresh_started_at = NULL,
                                    status = %(status)s,
                                    needs_reauth_at = CASE WHEN %(status)s = 'needs_reauth'
                                                           THEN COALESCE(needs_reauth_at, NOW())
                                                           ELSE NULL END,
                                    last_error_code = %(err_code)s,
                                    last_refresh_at = NOW(),
                                    updated_at = NOW()
                                WHERE user_id = %(user_id)s
                                  AND provider = 'google'
                                  AND service = %(service)s
                                  AND auth_epoch = %(auth_epoch)s
                                """,
                                {
                                    "access": token_resp.access_token,
                                    "refresh": token_resp.refresh_token,
                                    "enc_key": enc_key,
                                    "expires_at": token_resp.expires_at,
                                    "scopes": new_sib_scopes,
                                    "preferences": Jsonb(sib_prefs),
                                    "status": sib_status,
                                    "err_code": sib_err_code,
                                    "user_id": user_id,
                                    "service": sib.service,
                                    "auth_epoch": sib.auth_epoch,
                                },
                            )
                deferred_exc = IntegrationEpochRevokedError(
                    "Connection was disconnected during refresh."
                )
            elif cur.token_generation != start_token_gen:
                # CAS generation conflict branch (§4.2(2) step 3)
                if cur.status == IntegrationStatus.CONNECTED and cur.access_token:
                    committed_row = cur
                elif cur.status == IntegrationStatus.NEEDS_REAUTH:
                    deferred_exc = IntegrationNeedsReauthError(
                        "Connection transitioned to needs_reauth.",
                        code=cur.last_error_code or "needs_reauth",
                    )
                else:
                    deferred_exc = IntegrationEpochRevokedError(
                        "Connection state no longer servable after CAS conflict."
                    )
            elif cur.status not in (
                IntegrationStatus.CONNECTED,
                IntegrationStatus.REFRESH_AMBIGUOUS,
            ):
                deferred_exc = IntegrationNeedsReauthError(
                    "Connection requires re-authorization.",
                    code=cur.last_error_code or "needs_reauth",
                )
            else:
                # Normal CAS commit: authorized_at stays UNCHANGED (§4.2(2) item 5).
                # When scopes narrowed below minimum, we STILL commit the new token + token_generation + 1
                # together with status='needs_reauth' (old Feishu single-use refresh token may already be consumed).
                rows_to_update = [cur] + siblings
                async with tx_conn.cursor(row_factory=dict_row) as cur_sql:
                    for target in rows_to_update:
                        updated_scopes = (
                            _format_scopes(_parse_scopes(token_resp.scope))
                            if token_resp.scope
                            else target.granted_scopes
                        )
                        updated_prefs, _ = _converge_preferences_for_scopes(
                            provider,
                            target.service,
                            _parse_scopes(updated_scopes),
                            target.preferences,
                        )
                        missing_min = _missing_min_scopes(
                            provider, target.service, _parse_scopes(updated_scopes)
                        )
                        if missing_min:
                            new_status = IntegrationStatus.NEEDS_REAUTH.value
                            err_code = "missing_required_scopes"
                        else:
                            new_status = IntegrationStatus.CONNECTED.value
                            err_code = None

                        await cur_sql.execute(
                            f"""
                            UPDATE dalton_user_integrations
                            SET access_token = pgp_sym_encrypt(%(access)s, %(enc_key)s),
                                refresh_token = CASE WHEN %(refresh)s::text IS NULL THEN refresh_token
                                                     ELSE pgp_sym_encrypt(%(refresh)s, %(enc_key)s) END,
                                expires_at = %(expires_at)s,
                                granted_scopes = %(scopes)s,
                                preferences = %(preferences)s,
                                token_generation = token_generation + 1,
                                refresh_inflight_gen = NULL,
                                refresh_started_at = NULL,
                                status = %(status)s,
                                last_refresh_at = NOW(),
                                needs_reauth_at = CASE WHEN %(status)s = 'needs_reauth' THEN COALESCE(needs_reauth_at, NOW()) ELSE NULL END,
                                last_error_code = %(err_code)s,
                                updated_at = NOW()
                            WHERE user_id = %(user_id)s
                              AND provider = %(provider)s
                              AND service = %(service)s
                              AND oauth_client_id = %(client_id)s
                              AND provider_account_id = %(sub)s
                              AND auth_epoch = %(auth_epoch)s
                              AND token_generation = %(token_gen)s
                            RETURNING {db._INTEGRATION_META_COLS}
                            """,
                            {
                                "access": token_resp.access_token,
                                "refresh": token_resp.refresh_token,
                                "enc_key": enc_key,
                                "expires_at": token_resp.expires_at,
                                "scopes": updated_scopes,
                                "preferences": Jsonb(updated_prefs),
                                "status": new_status,
                                "err_code": err_code,
                                "user_id": user_id,
                                "provider": provider,
                                "service": target.service,
                                "client_id": target.oauth_client_id,
                                "sub": target.provider_account_id,
                                "auth_epoch": target.auth_epoch,
                                "token_gen": target.token_generation,
                            },
                        )
                        updated_dict = await cur_sql.fetchone()
                        if target.service == service and updated_dict is not None:
                            committed_row = db._row_to_integration(
                                {**dict(updated_dict), "decrypted_access_token": token_resp.access_token}
                            )

                if committed_row is None:
                    deferred_exc = IntegrationEpochRevokedError("CAS update matched zero rows.")

    # Transaction is now COMMITTED. Perform post-commit validation and raise business errors safely.
    if deferred_exc is not None:
        raise deferred_exc
    assert committed_row is not None
    if committed_row.status == IntegrationStatus.NEEDS_REAUTH:
        raise IntegrationNeedsReauthError(
            "Granted scopes no longer include the minimum required permissions.",
            code="missing_required_scopes",
        )
    _check_required_scopes(committed_row.granted_scopes, required_scopes)
    return _to_fresh_token(committed_row)


async def ensure_fresh_access_token(
    user_id: str,
    provider: str,
    service: str,
    *,
    min_remaining_seconds: int = 600,
    force_refresh: bool = False,
    required_scopes: Collection[str] | None = None,
    google_client: GoogleOAuthClientProtocol | None = None,
    feishu_client: FeishuOAuthClientProtocol | None = None,
    revoker: RemoteRevoker | None = None,
    waiter_budget_seconds: float = _DEFAULT_WAITER_BUDGET_SECONDS,
    waiter_poll_interval_seconds: float = _DEFAULT_WAITER_POLL_INTERVAL_SECONDS,
) -> FreshAccessToken:
    """Return a valid access token for ``(user_id, provider, service)``, refreshing serially across workers (§4.2(2))."""
    norm_provider = str(provider or "").strip().lower()
    norm_service = str(service or "").strip().lower()

    client_id, client_secret = _load_provider_oauth_credentials(norm_provider)

    row = await db.get_connection(
        user_id, norm_provider, norm_service, decrypt_tokens=True
    )
    if row is None:
        raise IntegrationNotConnectedError(
            f"No {norm_provider}/{norm_service} connection found.",
        )

    # Client ID consistency check (§4.2(1))
    if row.oauth_client_id != client_id:
        await db.update_connection_status(
            user_id,
            norm_provider,
            norm_service,
            status=IntegrationStatus.NEEDS_REAUTH,
            last_error_code="oauth_client_rotated",
            clear_tokens=False,
            expected_auth_epoch=row.auth_epoch,
        )
        raise IntegrationNeedsReauthError(
            "OAuth client was rotated by the administrator; please reconnect.",
            code="oauth_client_rotated",
        )

    if row.status == IntegrationStatus.REVOKED_PURGING:
        # Repair orphaned revoked_purging rows lacking a running 2A purge ledger covering this account/epoch (§4.2(2))
        running_purges = await db.has_running_local_purge_for_service(
            user_id,
            norm_provider,
            norm_service,
            oauth_client_id=row.oauth_client_id,
            provider_account_id=row.provider_account_id,
            min_auth_epoch_upto=row.auth_epoch,
        )
        if not running_purges:
            try:
                repair_pid = await begin_disconnect(
                    user_id,
                    norm_provider,
                    norm_service,
                    trigger_reason=TriggerReason.ERROR_CODE_REVOKED,
                    remote_mode="local_only",
                    expected_auth_epoch=row.auth_epoch,
                    clear_tokens=True,
                    skip_if_running_ledger=True,
                )
            except (DisconnectTargetMismatchError, IntegrationNotFoundError):
                raise IntegrationEpochRevokedError(
                    "Connection is disconnecting or revoked."
                ) from None
            try:
                await finish_disconnect(repair_pid, revoker=None)
            except Exception as exc:
                logger.warning(
                    "[integration-oauth] orphaned revoked_purging finish_disconnect deferred (%s)",
                    type(exc).__name__,
                )
            raise IntegrationRevokedError(
                "External grant was revoked by the user; personal data purge has been triggered.",
                code=row.last_error_code or "integration_revoked",
            )
        raise IntegrationEpochRevokedError("Connection is disconnecting or revoked.")

    if row.status == IntegrationStatus.DISCONNECT_PENDING:
        raise IntegrationEpochRevokedError("Connection is disconnecting or revoked.")
    if row.status == IntegrationStatus.NEEDS_REAUTH:
        raise IntegrationNeedsReauthError(
            "Connection requires re-authorization.",
            code=row.last_error_code or "needs_reauth",
        )

    start_client_id = row.oauth_client_id
    start_sub = row.provider_account_id
    start_auth_epoch = row.auth_epoch
    start_token_gen = row.token_generation

    # Fast path (§4.2(2) item 2)
    now = datetime.now(UTC)
    if (
        not force_refresh
        and row.status in (IntegrationStatus.CONNECTED, IntegrationStatus.REFRESH_AMBIGUOUS)
        and row.access_token
        and _remaining_seconds(row.expires_at, now) >= min_remaining_seconds
    ):
        _check_required_scopes(row.granted_scopes, required_scopes)
        return _to_fresh_token(row)

    # Step 1: Acquire non-blocking refresh advisory lock
    r_lock_key = refresh_lock_key(
        user_id,
        norm_provider,
        start_client_id,
        start_sub,
        norm_service if norm_provider == "feishu" else None,
    )
    trigger_revocation_after_unlock = False
    revocation_error_code: str | None = None
    winner_token_gen = start_token_gen

    async with get_db_connection() as lock_conn:
        async with lock_conn.cursor() as cur:
            await cur.execute("SELECT pg_try_advisory_lock(%s)", (r_lock_key,))
            lock_row = await cur.fetchone()
            won = bool(lock_row and lock_row[0])

        if not won:
            return await _wait_for_refresh_winner(
                user_id=user_id,
                provider=norm_provider,
                service=norm_service,
                start_client_id=start_client_id,
                start_sub=start_sub,
                start_auth_epoch=start_auth_epoch,
                start_token_gen=start_token_gen,
                min_remaining_seconds=min_remaining_seconds,
                force_refresh=force_refresh,
                required_scopes=required_scopes,
                budget_seconds=waiter_budget_seconds,
                poll_interval_seconds=waiter_poll_interval_seconds,
            )

        try:
            # Winner: short transaction to re-read, check crash recovery & mark refresh_inflight_gen.
            # Commit any status updates BEFORE raising business errors or returning.
            pre_return_row: IntegrationRow | None = None
            pre_exc: Exception | None = None
            winner_refresh_token: str | None = None
            winner_expires_at: datetime | None = None
            winner_access_token: str | None = None
            winner_granted_scopes: str = ""

            async with get_db_connection() as pre_conn, pre_conn.transaction():
                current = await db.get_connection(
                    user_id,
                    norm_provider,
                    norm_service,
                    decrypt_tokens=True,
                    for_update=True,
                    conn=pre_conn,
                )
                if current is None or current.status in (
                    IntegrationStatus.DISCONNECT_PENDING,
                    IntegrationStatus.REVOKED_PURGING,
                ):
                    pre_exc = IntegrationEpochRevokedError("Connection was disconnected.")
                elif (
                    current.oauth_client_id != start_client_id
                    or current.provider_account_id != start_sub
                    or current.auth_epoch != start_auth_epoch
                ):
                    pre_exc = IntegrationEpochRevokedError(
                        "Connection identity changed while waiting for refresh lock."
                    )
                elif current.status == IntegrationStatus.NEEDS_REAUTH:
                    pre_exc = IntegrationNeedsReauthError(
                        "Connection requires re-authorization.",
                        code=current.last_error_code or "needs_reauth",
                    )
                else:
                    now_inner = datetime.now(UTC)
                    rem_inner = _remaining_seconds(current.expires_at, now_inner)
                    # Crash recovery for Feishu: leftover inflight marker from a crashed worker
                    # means the single-use refresh token may already have been consumed upstream.
                    # NEVER replay it: converge to refresh_ambiguous (while access token is still
                    # valid) or directly to needs_reauth (once access token has expired).
                    if (
                        norm_provider == "feishu"
                        and current.refresh_inflight_gen == current.token_generation
                    ):
                        if current.expires_at is not None and rem_inner > 0:
                            await db.update_connection_status(
                                user_id,
                                norm_provider,
                                norm_service,
                                status=IntegrationStatus.REFRESH_AMBIGUOUS,
                                last_error_code="previous_refresh_crashed",
                                clear_tokens=False,
                                expected_auth_epoch=current.auth_epoch,
                                conn=pre_conn,
                            )
                            if not force_refresh and current.access_token:
                                pre_return_row = current
                            else:
                                pre_exc = IntegrationTransientError(
                                    "Feishu token refresh outcome was ambiguous; reconnect if needed.",
                                    code="refresh_ambiguous",
                                )
                        else:
                            await db.update_connection_status(
                                user_id,
                                norm_provider,
                                norm_service,
                                status=IntegrationStatus.NEEDS_REAUTH,
                                last_error_code="previous_refresh_crashed",
                                clear_tokens=True,
                                expected_auth_epoch=current.auth_epoch,
                                conn=pre_conn,
                            )
                            pre_exc = IntegrationNeedsReauthError(
                                "Previous Feishu refresh crashed after access token expiry; please reconnect.",
                                code="previous_refresh_crashed",
                            )
                    elif (
                        norm_provider == "feishu"
                        and current.status == IntegrationStatus.REFRESH_AMBIGUOUS
                        and current.expires_at is not None
                        and rem_inner > 0
                    ):
                        # Already in refresh_ambiguous with a still-valid access token:
                        # do not replay the possibly-consumed refresh token before expiry.
                        if not force_refresh and current.access_token:
                            pre_return_row = current
                        else:
                            pre_exc = IntegrationTransientError(
                                "Feishu token refresh outcome was ambiguous; reconnect if needed.",
                                code="refresh_ambiguous",
                            )
                    else:
                        # Reuse a newly committed token (including for force_refresh when
                        # token_generation already advanced past our start_token_gen!)
                        already_refreshed = (
                            current.status == IntegrationStatus.CONNECTED
                            and bool(current.access_token)
                            and rem_inner >= min_remaining_seconds
                            and (
                                not force_refresh
                                or current.token_generation > start_token_gen
                            )
                        )
                        if already_refreshed:
                            pre_return_row = current
                        elif not current.refresh_token:
                            await db.update_connection_status(
                                user_id,
                                norm_provider,
                                norm_service,
                                status=IntegrationStatus.NEEDS_REAUTH,
                                last_error_code="missing_refresh_token",
                                clear_tokens=True,
                                expected_auth_epoch=current.auth_epoch,
                                conn=pre_conn,
                            )
                            pre_exc = IntegrationNeedsReauthError(
                                "Missing refresh token; please reconnect.",
                                code="missing_refresh_token",
                            )
                        else:
                            # Record inflight refresh marker before leaving DB transaction
                            winner_token_gen = current.token_generation
                            winner_refresh_token = current.refresh_token
                            winner_expires_at = current.expires_at
                            winner_access_token = current.access_token
                            winner_granted_scopes = current.granted_scopes

                            async with pre_conn.cursor() as cur_sql:
                                await cur_sql.execute(
                                    """
                                    UPDATE dalton_user_integrations
                                    SET refresh_inflight_gen = %s,
                                        refresh_started_at = NOW(),
                                        updated_at = NOW()
                                    WHERE user_id = %s
                                      AND provider = %s
                                      AND service = %s
                                      AND auth_epoch = %s
                                      AND token_generation = %s
                                    """,
                                    (
                                        winner_token_gen,
                                        user_id,
                                        norm_provider,
                                        norm_service,
                                        start_auth_epoch,
                                        winner_token_gen,
                                    ),
                                )

            if pre_exc is not None:
                raise pre_exc
            if pre_return_row is not None:
                _check_required_scopes(pre_return_row.granted_scopes, required_scopes)
                return _to_fresh_token(pre_return_row)

            assert winner_refresh_token is not None

            # Step 2: Upstream HTTP refresh OUTSIDE DB transaction (§4.2(2) step 2)
            upstream_err: OAuthUpstreamError | None = None
            token_resp: OAuthTokenResponse | None = None
            try:
                if norm_provider == "google":
                    g_client = google_client or get_google_oauth_client()
                    token_resp = await g_client.refresh_token(
                        client_id=client_id,
                        client_secret=client_secret,
                        refresh_token=winner_refresh_token,
                    )
                else:
                    f_client = feishu_client or get_feishu_oauth_client()
                    token_resp = await f_client.refresh_token(
                        app_id=client_id,
                        app_secret=client_secret,
                        refresh_token=winner_refresh_token,
                    )
            except OAuthUpstreamError as exc:
                upstream_err = exc

            # Step 3: Commit success or handle classified error
            if token_resp is not None:
                attempts = _FEISHU_DB_COMMIT_RETRIES if norm_provider == "feishu" else 1
                last_db_exc: Exception | None = None
                for attempt in range(attempts):
                    try:
                        return await _commit_refresh_success_tx(
                            user_id=user_id,
                            provider=norm_provider,
                            service=norm_service,
                            start_client_id=start_client_id,
                            start_sub=start_sub,
                            start_auth_epoch=start_auth_epoch,
                            start_token_gen=winner_token_gen,
                            token_resp=token_resp,
                            required_scopes=required_scopes,
                        )
                    except PersonalIntegrationError:
                        raise
                    except Exception as exc:
                        last_db_exc = exc
                        if attempt + 1 < attempts:
                            await asyncio.sleep(0.1 * (attempt + 1))
                assert last_db_exc is not None
                raise last_db_exc

            assert upstream_err is not None

            # Verify 3-element identity & token_generation before mutating on upstream error.
            # Commit ALL state transitions BEFORE raising business exceptions!
            err_return_token: FreshAccessToken | None = None
            err_return_scopes: str | None = None
            err_to_raise: Exception | None = None

            async with get_db_connection() as err_conn, err_conn.transaction():
                latest = await db.get_connection(
                    user_id,
                    norm_provider,
                    norm_service,
                    decrypt_tokens=False,
                    for_update=True,
                    conn=err_conn,
                )
                if latest is None or (
                    latest.oauth_client_id != start_client_id
                    or latest.provider_account_id != start_sub
                    or latest.auth_epoch != start_auth_epoch
                ):
                    err_to_raise = IntegrationEpochRevokedError(
                        "Connection changed during failed refresh."
                    )
                elif latest.token_generation != winner_token_gen:
                    # Another commit advanced token_generation; do not overwrite it
                    if latest.status == IntegrationStatus.CONNECTED:
                        full_latest = await db.get_connection(
                            user_id,
                            norm_provider,
                            norm_service,
                            decrypt_tokens=True,
                            conn=err_conn,
                        )
                        if full_latest and full_latest.access_token:
                            err_return_token = _to_fresh_token(full_latest)
                            err_return_scopes = full_latest.granted_scopes
                    if err_return_token is None:
                        err_to_raise = IntegrationEpochRevokedError(
                            "Connection generation moved during failed refresh."
                        )
                elif upstream_err.kind == "revoked":
                    # Official confirmed user-revoked error code:
                    # Do NOT transition to revoked_purging or clear tokens here; only clear
                    # the inflight marker and let begin_disconnect atomically set revoked_purging
                    # + clear tokens + lock threads + insert purge ledger in ONE transaction
                    # AFTER releasing refresh_lock (strict lifecycle -> refresh order).
                    async with err_conn.cursor() as cur_sql:
                        await cur_sql.execute(
                            """
                            UPDATE dalton_user_integrations
                            SET refresh_inflight_gen = NULL,
                                refresh_started_at = NULL,
                                updated_at = NOW()
                            WHERE user_id = %s
                              AND provider = %s
                              AND service = %s
                              AND auth_epoch = %s
                              AND token_generation = %s
                            """,
                            (
                                user_id,
                                norm_provider,
                                norm_service,
                                start_auth_epoch,
                                winner_token_gen,
                            ),
                        )
                    trigger_revocation_after_unlock = True
                    revocation_error_code = upstream_err.error_code
                elif upstream_err.kind in ("invalid_grant", "needs_reauth"):
                    # Routine credential failure -> needs_reauth ONLY (keep online conversation history!)
                    if norm_provider == "google":
                        # All active Google siblings sharing (oauth_client_id, sub) share this invalidated refresh token
                        sibling_rows = await db.list_connections_by_account(
                            user_id,
                            "google",
                            start_client_id,
                            start_sub,
                            decrypt_tokens=False,
                            for_update=True,
                            conn=err_conn,
                        )
                        targets_to_mark = [
                            r
                            for r in sibling_rows
                            if r.status
                            in (
                                IntegrationStatus.CONNECTED,
                                IntegrationStatus.REFRESH_AMBIGUOUS,
                            )
                        ]
                        if not any(r.service == norm_service for r in targets_to_mark):
                            targets_to_mark.append(latest)
                        for t_row in targets_to_mark:
                            await db.update_connection_status(
                                user_id,
                                norm_provider,
                                t_row.service,
                                status=IntegrationStatus.NEEDS_REAUTH,
                                last_error_code=upstream_err.error_code,
                                clear_tokens=True,
                                expected_auth_epoch=t_row.auth_epoch,
                                conn=err_conn,
                            )
                    else:
                        await db.update_connection_status(
                            user_id,
                            norm_provider,
                            norm_service,
                            status=IntegrationStatus.NEEDS_REAUTH,
                            last_error_code=upstream_err.error_code,
                            clear_tokens=True,
                            expected_auth_epoch=start_auth_epoch,
                            conn=err_conn,
                        )
                    err_to_raise = IntegrationNeedsReauthError(
                        "Integration authorization expired or became invalid; please reconnect.",
                        code=upstream_err.error_code,
                    )
                elif upstream_err.kind == "ambiguous" and norm_provider == "feishu":
                    now_err = datetime.now(UTC)
                    if (
                        winner_expires_at is not None
                        and _remaining_seconds(winner_expires_at, now_err) > 0
                    ):
                        await db.update_connection_status(
                            user_id,
                            norm_provider,
                            norm_service,
                            status=IntegrationStatus.REFRESH_AMBIGUOUS,
                            last_error_code=upstream_err.error_code,
                            clear_tokens=False,
                            expected_auth_epoch=start_auth_epoch,
                            conn=err_conn,
                        )
                        if not force_refresh and winner_access_token:
                            err_return_token = FreshAccessToken(
                                access_token=winner_access_token,
                                provider=norm_provider,
                                service=norm_service,
                                oauth_client_id=start_client_id,
                                provider_account_id=start_sub,
                                auth_epoch=start_auth_epoch,
                                token_generation=winner_token_gen,
                                granted_scopes=winner_granted_scopes,
                                expires_at=winner_expires_at,
                            )
                            err_return_scopes = winner_granted_scopes
                        else:
                            err_to_raise = IntegrationTransientError(
                                "Feishu token refresh outcome was ambiguous; reconnect if needed.",
                                code="refresh_ambiguous",
                            )
                    else:
                        # Access token already expired -> converge directly to needs_reauth (§4.2(2) item 5)
                        await db.update_connection_status(
                            user_id,
                            norm_provider,
                            norm_service,
                            status=IntegrationStatus.NEEDS_REAUTH,
                            last_error_code="refresh_timeout_after_expiry",
                            clear_tokens=True,
                            expected_auth_epoch=start_auth_epoch,
                            conn=err_conn,
                        )
                        err_to_raise = IntegrationNeedsReauthError(
                            "Feishu refresh timed out after access token expiry; please reconnect.",
                            code="refresh_timeout_after_expiry",
                        )
                else:
                    # Transient error (timeout / 429 / 5xx) -> clear inflight marker, keep connected
                    async with err_conn.cursor() as cur_sql:
                        await cur_sql.execute(
                            """
                            UPDATE dalton_user_integrations
                            SET refresh_inflight_gen = NULL,
                                refresh_started_at = NULL,
                                last_error_code = %s,
                                updated_at = NOW()
                            WHERE user_id = %s
                              AND provider = %s
                              AND service = %s
                              AND auth_epoch = %s
                            """,
                            (
                                upstream_err.error_code,
                                user_id,
                                norm_provider,
                                norm_service,
                                start_auth_epoch,
                            ),
                        )
                    err_to_raise = IntegrationTransientError(
                        "Temporary upstream OAuth error; retry shortly.",
                        code=upstream_err.error_code,
                    )

            # Error transaction is now COMMITTED. Return or raise outside the transaction.
            if err_return_token is not None:
                _check_required_scopes(err_return_scopes or "", required_scopes)
                return err_return_token
            if err_to_raise is not None:
                raise err_to_raise
        finally:
            with anyio.CancelScope(shield=True):
                await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (r_lock_key,))

    # Outside refresh lock: run atomic lifecycle Phase 1 (revoked_purging + clear tokens + thread lock + ledger)
    # in ONE transaction, propagating Phase 1 failures cleanly without swallowing!
    if trigger_revocation_after_unlock:
        try:
            purge_id = await begin_disconnect(
                user_id,
                norm_provider,
                norm_service,
                trigger_reason=TriggerReason.ERROR_CODE_REVOKED,
                remote_mode="local_only",
                expected_oauth_client_id=start_client_id,
                expected_provider_account_id=start_sub,
                expected_auth_epoch=start_auth_epoch,
                expected_token_generation=winner_token_gen,
                last_error_code=revocation_error_code,
                clear_tokens=True,
            )
        except (DisconnectTargetMismatchError, IntegrationNotFoundError):
            raise IntegrationEpochRevokedError(
                "Connection changed before revocation disconnect registration."
            ) from None

        try:
            await finish_disconnect(purge_id, revoker=None)
        except Exception as exc:
            logger.warning(
                "[integration-oauth] revocation finish_disconnect deferred after Phase 1 commit (%s)",
                type(exc).__name__,
            )
        raise IntegrationRevokedError(
            "External grant was revoked by the user; personal data purge has been triggered.",
            code=revocation_error_code or "integration_revoked",
        )

    raise IntegrationTransientError("Unexpected refresh exit state.")


__all__ = [
    "ConfirmIntegrationResult",
    "FeishuIdentityMismatchError",
    "FreshAccessToken",
    "IntegrationCredentialsNotReadyError",
    "IntegrationEpochRevokedError",
    "IntegrationNeedsReauthError",
    "IntegrationNotConnectedError",
    "IntegrationRefreshBusyError",
    "IntegrationRevokedError",
    "IntegrationTransientError",
    "MissingRefreshTokenError",
    "MissingRefreshTokenOnClientRotationError",
    "MissingRequiredScopesError",
    "OAUTH_BROWSER_BINDING_COOKIE_NAME",
    "OAuthCallbackResult",
    "OAuthSessionInvalidError",
    "OAuthStateInvalidError",
    "PendingConfirmationInvalidError",
    "PersonalIntegrationError",
    "StartAuthorizationResult",
    "confirm_pending_integration",
    "ensure_fresh_access_token",
    "handle_oauth_callback",
    "sanitize_return_to",
    "start_oauth_authorization",
    "validate_callback_location",
]
