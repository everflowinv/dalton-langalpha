"""Personal integration OAuth connect, callback, confirmation, and serialized CAS refresh (§4.2).

Deployment credentials are read exclusively via ``get_credential_group(...)``.
All concurrency state (one-time OAuth ``state``, pending confirmations, lifecycle /
refresh / revoke advisory locks) lives in PostgreSQL for multi-worker safety.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Collection
from contextlib import ExitStack
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
    google_project_coord_lock_key,
    lifecycle_lock_key,
    refresh_lock_key,
    revoke_lock_key,
)
from src.dalton.deployment_credentials.catalog import (
    FEISHU_APP_ID_KEY,
    FEISHU_APP_SECRET_KEY,
    GOOGLE_OAUTH_CLIENT_ID_KEY,
    GOOGLE_OAUTH_CLIENT_SECRET_KEY,
)
from src.dalton.deployment_credentials.redirect_uri import google_redirect_uri_eligible
from src.dalton.deployment_credentials.snapshot import get_credential_group
from src.dalton.integrations.lock_guard import (
    assert_no_advisory_lock_held,
    track_advisory_lock,
)
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
    process_revoke,
    revoke_before_new_authorization,
)
from src.dalton.services.user_integrations_lifecycle import (
    AccountSwitchTargetChangedError,
    DisconnectTargetMismatchError,
    GoogleAccountAlreadyConnectedError,
    IntegrationNotFoundError,
    MissingRefreshTokenOnAccountSwitchError,
    begin_disconnect,
    finish_disconnect,
    finish_disconnect_locked,
    replace_connection_for_account_switch,
    sync_google_sibling_rows_tx,
)
from src.dalton.settings import get_dalton_auth_mode, get_dalton_public_app_url
from src.server.database.encryption import encryption_configured, get_encryption_key
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_DEFAULT_WAITER_BUDGET_SECONDS = 12.0
_DEFAULT_WAITER_POLL_INTERVAL_SECONDS = 0.2
_FEISHU_DB_COMMIT_RETRIES = 3
_REFRESH_INFLIGHT_LEASE_SECONDS = 25
_GOOGLE_EXCHANGE_LEASE_SECONDS = 30
_OAUTH_EXCHANGE_TOTAL_TIMEOUT_SECONDS = 20.0
_GOOGLE_ACCOUNT_OWNED_BY_OTHER_MEMBER_MSG = (
    "这个 Google 账号已被团队里另一位成员连接，请换一个账号或请对方先断开"
)

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


class GoogleAccountAlreadyConnectedByOtherMemberError(PersonalIntegrationError):
    status_code = 409
    code = "google_account_already_connected"

    def __init__(
        self,
        message: str = _GOOGLE_ACCOUNT_OWNED_BY_OTHER_MEMBER_MSG,
    ) -> None:
        super().__init__(
            message,
            code="google_account_already_connected",
            status_code=409,
        )


class OAuthExchangeLeaseExpiredError(PersonalIntegrationError):
    status_code = 409
    code = "exchange_lease_expired"


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
    """Return the Feishu App ID actively used by the channel gateway, or None when disabled/unavailable (§4.3 item 2).

    1. When a live channel gateway has reported its active ``feishu_app_id`` via the
       deployment lease (``dalton_integration_gateway_runtime``), returns that exact
       in-use App ID.
    2. Otherwise falls back strictly to ``get_credential_group("feishu_app")``
       (never reading environment variables or live Vault directly).
    3. When ``CHANNEL_FEISHU`` is enabled but the gateway's App ID cannot be determined,
       logs a warning and returns ``None``.
    """
    if not has_capability(CapabilityId.CHANNEL_FEISHU):
        return None
    has_live_gw, live_app_id = await db.get_active_channel_gateway_feishu_app_id()
    if has_live_gw:
        if live_app_id:
            return live_app_id
        logger.warning(
            "[integration-oauth] live channel gateway reported no active Feishu App ID; skipping open_id check"
        )
        return None
    feishu_group = get_credential_group("feishu_app")
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
    """Coordinate old Google grant revocation under ``(google_project, sub)`` before issuing state (§4.2(1)(3)).

    Never holds a PostgreSQL advisory lock or pool connection across upstream HTTP.
    """
    target_project = extract_google_project(target_client_id)

    # 1. Attempt any pending / failed_retryable ledger tasks in the same Google project
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
        await revoke_before_new_authorization(
            target_project, sub, revoker, user_id=user_id
        )

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
            lkey = revoke_lock_key(target_project, sub)
            rotation_pid: str | None = None

            # Short tx on ONE pooled connection: check guards, record ledger & clear old tokens
            async with get_db_connection() as tx_conn, tx_conn.transaction():
                await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (lkey,))
                with track_advisory_lock("revoke", lkey):
                    latest_rows = await db.list_user_connections(
                        user_id, provider="google", conn=tx_conn
                    )
                    other_on_old = [
                        r
                        for r in latest_rows
                        if r.service != service
                        and r.oauth_client_id == old_client_id
                        and r.provider_account_id == sub
                        and r.status
                        in (
                            IntegrationStatus.CONNECTED,
                            IntegrationStatus.REFRESH_AMBIGUOUS,
                        )
                    ]
                    if other_on_old:
                        return

                    has_newer_on_project = await db.has_active_or_pending_google_grant(
                        target_project,
                        sub,
                        user_id=user_id,
                        exclude_client_id=old_client_id,
                        conn=tx_conn,
                    )
                    if has_newer_on_project:
                        await db.supersede_active_revokes_for_google_project_sub_tx(
                            target_project,
                            sub,
                            reason="superseded_by_active_project_grant",
                            conn=tx_conn,
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
                            conn=tx_conn,
                        )
                        return

                    full_row = await db.get_connection(
                        user_id, "google", service, decrypt_tokens=True, conn=tx_conn
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

                    rotation_pid = str(uuid4())
                    await db.insert_purge_ledger(
                        purge_id=rotation_pid,
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
                        conn=tx_conn,
                    )
                    await db.update_connection_status(
                        user_id,
                        "google",
                        service,
                        status=IntegrationStatus.NEEDS_REAUTH,
                        last_error_code="oauth_client_rotated",
                        clear_tokens=True,
                        expected_auth_epoch=full_row.auth_epoch,
                        conn=tx_conn,
                    )

            # Execute remote revoke OUTSIDE the advisory lock via shared state machine
            if rotation_pid is not None:
                await process_revoke(rotation_pid, revoker)


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

    expected_sub: str | None = None
    if norm_provider == "google" and not switch_account:
        target_proj = extract_google_project(client_id)
        if (
            target_existing is not None
            and extract_google_project(target_existing.oauth_client_id) == target_proj
        ):
            expected_sub = target_existing.provider_account_id
        else:
            same_proj_active = [
                r
                for r in existing_rows
                if extract_google_project(r.oauth_client_id) == target_proj
                and r.status
                in (IntegrationStatus.CONNECTED, IntegrationStatus.REFRESH_AMBIGUOUS)
            ]
            if same_proj_active:
                expected_sub = same_proj_active[0].provider_account_id

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
        expected_provider_account_id=expected_sub,
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
    consume_state_hash: str | None = None,
    baseline_existed: bool | None = None,
) -> None:
    """Atomically verify connect intent/exchange lease/revocation claim/ownership and stage a pending confirmation on ONE pooled connection.

    Canonical lock order (all ``pg_advisory_xact_lock`` on ``tx_conn``):
      ``lifecycle_lock_key(user_id, provider)`` -> ``revoke_lock_key(google_project, sub)``
      -> ``google_project_coord_lock_key(google_project)``.
    """
    life_key = lifecycle_lock_key(user_id, provider)
    google_project = (
        extract_google_project(oauth_client_id) if provider == "google" else None
    )
    rkey = (
        revoke_lock_key(google_project, provider_account_id)
        if google_project is not None
        else None
    )
    coord_key = (
        google_project_coord_lock_key(google_project)
        if google_project is not None
        else None
    )

    async with get_db_connection() as tx_conn, tx_conn.transaction():
        with ExitStack() as lock_stack:
            await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (life_key,))
            lock_stack.enter_context(track_advisory_lock("lifecycle", life_key))
            if rkey is not None:
                await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (rkey,))
                lock_stack.enter_context(track_advisory_lock("revoke", rkey))
            if coord_key is not None:
                await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (coord_key,))
                lock_stack.enter_context(
                    track_advisory_lock("google_project_coord", coord_key)
                )

            if consume_state_hash is not None:
                intent_status, _intent_row = await db.consume_active_oauth_intent_tx(
                    consume_state_hash,
                    provider,
                    require_valid_exchange_lease=(provider == "google"),
                    conn=tx_conn,
                )
                if intent_status in ("voided", "missing"):
                    raise PendingConfirmationInvalidError(
                        "Connection was disconnected during OAuth callback; authorization discarded.",
                        code="connection_disconnected_during_callback",
                        status_code=409,
                    )
                if intent_status == "lease_expired":
                    raise OAuthExchangeLeaseExpiredError(
                        "OAuth exchange protection lease expired before staging; please retry."
                    )

            if google_project is not None:
                if await db.has_inflight_google_revoke_for_project_sub(
                    google_project, provider_account_id, conn=tx_conn
                ):
                    raise OAuthExchangeLeaseExpiredError(
                        "A remote revocation claimed execution before staging; please retry."
                    )
                if await db.has_other_user_google_account_claim(
                    google_project,
                    provider_account_id,
                    exclude_user_id=user_id,
                    conn=tx_conn,
                ):
                    raise GoogleAccountAlreadyConnectedByOtherMemberError()

            if baseline_existed is True:
                cur_row = await db.get_connection(
                    user_id,
                    provider,
                    service,
                    decrypt_tokens=False,
                    for_update=True,
                    conn=tx_conn,
                )
                baseline_ok = (
                    cur_row is not None
                    and cur_row.status
                    not in (
                        IntegrationStatus.DISCONNECT_PENDING,
                        IntegrationStatus.REVOKED_PURGING,
                    )
                    and (
                        expected_old_oauth_client_id is None
                        or cur_row.oauth_client_id == expected_old_oauth_client_id
                    )
                    and (
                        expected_old_provider_account_id is None
                        or cur_row.provider_account_id == expected_old_provider_account_id
                    )
                    and (
                        expected_old_auth_epoch is None
                        or cur_row.auth_epoch == int(expected_old_auth_epoch)
                    )
                )
                if not baseline_ok:
                    raise PendingConfirmationInvalidError(
                        "Connection was disconnected or changed during OAuth callback; authorization discarded.",
                        code="connection_disconnected_during_callback",
                        status_code=409,
                    )

            if google_project is not None:
                await db.supersede_active_revokes_for_google_project_sub_tx(
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
    consume_confirmation_hash: str | None = None,
    consume_state_hash: str | None = None,
    baseline_existed: bool | None = None,
) -> tuple[IntegrationRow, str | None]:
    """Commit a verified OAuth grant into ``dalton_user_integrations`` using ONE pooled connection.

    Handles all 3 Google cases (first connect, same-sub client rotation rebind preserving
    history, confirmed account switch reusing Step 1 Phase 1 + 2A purge) and Feishu connect.
    Coordinates under ``pg_advisory_xact_lock`` in canonical unidirectional order
    (``lifecycle -> refresh -> revoke -> google_project_coord``):
      - Atomically consumes ``consume_state_hash`` (direct callback intent) or
        ``consume_confirmation_hash`` (staged confirmation) INSIDE the activation transaction.
      - Re-verifies Google exchange lease and absence of in-flight revokes when consuming
        a direct-commit ``consume_state_hash``.
      - Enforces the 1-Google-account-per-Dalton-member invariant.
      - Supersedes active revokes for ``(google_project, sub)`` BEFORE committing.
      - Re-verifies ``baseline_existed`` and ``expected_old_*`` under ``FOR UPDATE``.
    Returns ``(connection_row, purge_id_if_account_switch)``.
    """
    # Fast-fail before waiting on any running purge if a concurrent disconnect already voided this intent
    if consume_state_hash is not None and await db.peek_oauth_state_voided(
        consume_state_hash, provider
    ):
        raise PendingConfirmationInvalidError(
            "Connection was disconnected during OAuth callback; authorization discarded.",
            code="connection_disconnected_during_callback",
            status_code=409,
        )

    await _wait_for_running_service_purge(user_id, provider, service)

    effective_consume_hash = consume_confirmation_hash or guard_confirmation_hash
    has_expected_old = (
        expected_old_oauth_client_id is not None
        or expected_old_provider_account_id is not None
        or expected_old_auth_epoch is not None
        or baseline_existed is True
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
                consume_confirmation_hash=effective_consume_hash,
            )
        except GoogleAccountAlreadyConnectedError:
            raise GoogleAccountAlreadyConnectedByOtherMemberError() from None
        except (AccountSwitchTargetChangedError, IntegrationNotFoundError):
            if has_expected_old or effective_consume_hash is not None:
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
    life_key = lifecycle_lock_key(user_id, provider)
    google_project = (
        extract_google_project(oauth_client_id) if provider == "google" else None
    )
    revoke_key = (
        revoke_lock_key(google_project, provider_account_id)
        if google_project is not None
        else None
    )
    coord_key = (
        google_project_coord_lock_key(google_project)
        if google_project is not None and consume_state_hash is not None
        else None
    )
    cross_project_revoke_purge_id: str | None = None

    async with get_db_connection() as tx_conn, tx_conn.transaction():
        with ExitStack() as lock_stack:
            await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (life_key,))
            lock_stack.enter_context(track_advisory_lock("lifecycle", life_key))
            # Determine refresh lock key(s) in deterministic ascending order (lifecycle -> refresh -> revoke -> coord)
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
            for rk in sorted(r_keys_set):
                await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (rk,))
                lock_stack.enter_context(track_advisory_lock("refresh", rk))

            if revoke_key is not None:
                await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (revoke_key,))
                lock_stack.enter_context(track_advisory_lock("revoke", revoke_key))
            if coord_key is not None:
                await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (coord_key,))
                lock_stack.enter_context(
                    track_advisory_lock("google_project_coord", coord_key)
                )

            # Atomically consume & verify pending connect intent (direct callback) OR
            # staged pending confirmation INSIDE the activation transaction.
            if consume_state_hash is not None:
                intent_status, _intent_row = await db.consume_active_oauth_intent_tx(
                    consume_state_hash,
                    provider,
                    require_valid_exchange_lease=(provider == "google"),
                    conn=tx_conn,
                )
                if intent_status in ("voided", "missing"):
                    raise PendingConfirmationInvalidError(
                        "Connection was disconnected during OAuth callback; authorization discarded.",
                        code="connection_disconnected_during_callback",
                        status_code=409,
                    )
                if intent_status == "lease_expired":
                    raise OAuthExchangeLeaseExpiredError(
                        "OAuth exchange protection lease expired before activation; please retry."
                    )
                if google_project is not None and await db.has_inflight_google_revoke_for_project_sub(
                    google_project, provider_account_id, conn=tx_conn
                ):
                    raise OAuthExchangeLeaseExpiredError(
                        "A remote revocation claimed execution before activation; please retry."
                    )
            elif effective_consume_hash is not None:
                consumed_ticket = await db.consume_pending_confirmation(
                    effective_consume_hash,
                    user_id,
                    conn=tx_conn,
                )
                if consumed_ticket is None:
                    if guard_confirmation_hash is not None and consume_confirmation_hash is None:
                        raise PendingConfirmationInvalidError(
                            "Connection was disconnected during OAuth callback; authorization discarded.",
                            code="connection_disconnected_during_callback",
                            status_code=409,
                        )
                    raise PendingConfirmationInvalidError(
                        "Confirmation token is invalid, expired, or already used.",
                        code="invalid_or_expired_confirmation",
                        status_code=400,
                    )

            # Enforce 1-Google-account-per-Dalton-member invariant atomically under revoke_lock_key
            if google_project is not None:
                if await db.has_other_user_google_account_claim(
                    google_project,
                    provider_account_id,
                    exclude_user_id=user_id,
                    conn=tx_conn,
                ):
                    raise GoogleAccountAlreadyConnectedByOtherMemberError()
                await db.supersede_active_revokes_for_google_project_sub_tx(
                    google_project,
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
            if has_expected_old:
                old_matches = (
                    existing is not None
                    and existing.status
                    not in (
                        IntegrationStatus.DISCONNECT_PENDING,
                        IntegrationStatus.REVOKED_PURGING,
                    )
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
                    err_code = (
                        "connection_disconnected_during_callback"
                        if consume_state_hash is not None
                        else "account_switch_state_changed"
                    )
                    raise PendingConfirmationInvalidError(
                        "Connected account changed before confirmation; authorization discarded.",
                        code=err_code,
                        status_code=409,
                    )
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

            if provider == "google" and effective_refresh and active_new_siblings:
                await sync_google_sibling_rows_tx(
                    tx_conn,
                    user_id=user_id,
                    oauth_client_id=oauth_client_id,
                    provider_account_id=provider_account_id,
                    access_token=access_token,
                    refresh_token=effective_refresh,
                    expires_at=expires_at,
                    granted_scopes=granted_scopes,
                    active_siblings=active_new_siblings,
                )

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


_INFLIGHT_REVOKE_WAIT_TIMEOUT_S = 15.0
_INFLIGHT_REVOKE_POLL_INTERVAL_S = 0.05


async def _wait_for_inflight_google_revokes(
    google_project: str,
    *,
    timeout_s: float = _INFLIGHT_REVOKE_WAIT_TIMEOUT_S,
    poll_interval_s: float = _INFLIGHT_REVOKE_POLL_INTERVAL_S,
) -> bool:
    """Wait (without holding any DB lock or pool connection) for any already in-flight Google revoke HTTP call in ``google_project`` to settle.

    Returns ``True`` once no revoke HTTP attempt is in-flight, or ``False`` on timeout.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.1, float(timeout_s))
    while True:
        if not await db.has_inflight_google_revoke_in_project(google_project):
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll_interval_s, remaining))


async def _claim_google_oauth_state_coordinated(
    state_hash: str,
    *,
    error: str | None,
    code: str | None,
    exchange_lease_seconds: int = _GOOGLE_EXCHANGE_LEASE_SECONDS,
    timeout_s: float = _INFLIGHT_REVOKE_WAIT_TIMEOUT_S,
    poll_interval_s: float = _INFLIGHT_REVOKE_POLL_INTERVAL_S,
) -> tuple[db.OAuthStateRow | None, bool, bool]:
    """Atomically coordinate Google OAuth state consumption with lifecycle & project-level in-flight revokes on ONE connection.

    Returns ``(state_row, revoke_timed_out, intent_voided)``.
    Under short ``pg_advisory_xact_lock`` (canonical order ``lifecycle -> google_project_coord``):
      - If any Google revoke HTTP attempt in the same project currently holds an unexpired
        ``revoke_inflight_until`` lease, commits/releases the connection, sleeps briefly
        outside any lock, and retries until settled or ``timeout_s`` elapses.
      - Once no revoke is in-flight, atomically consumes ``state`` (recording ``consumed_at``,
        ``exchange_expires_at``, and ``baseline_*`` snapshot) inside the SAME transaction.
    """
    if error or not code or not str(code).strip():
        consumed = await db.consume_oauth_state(
            state_hash, "google", exchange_ttl_seconds=exchange_lease_seconds
        )
        if consumed is None and await db.peek_oauth_state_voided(state_hash, "google"):
            return None, False, True
        return consumed, False, False

    peeked = await db.peek_oauth_state(state_hash, "google")
    if peeked is None:
        if await db.peek_oauth_state_voided(state_hash, "google"):
            return None, False, True
        consumed = await db.consume_oauth_state(
            state_hash, "google", exchange_ttl_seconds=exchange_lease_seconds
        )
        return consumed, False, False

    peek_uid, peeked_client_id = peeked
    google_project = extract_google_project(peeked_client_id)
    life_key = lifecycle_lock_key(peek_uid, "google")
    coord_key = google_project_coord_lock_key(google_project)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.1, float(timeout_s))

    while True:
        async with get_db_connection() as tx_conn, tx_conn.transaction():
            with ExitStack() as lock_stack:
                await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (life_key,))
                lock_stack.enter_context(track_advisory_lock("lifecycle", life_key))
                await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (coord_key,))
                lock_stack.enter_context(
                    track_advisory_lock("google_project_coord", coord_key)
                )
                inflight = await db.has_inflight_google_revoke_in_project(
                    google_project, conn=tx_conn
                )
                if not inflight:
                    consumed = await db.consume_oauth_state(
                        state_hash,
                        "google",
                        exchange_ttl_seconds=exchange_lease_seconds,
                        conn=tx_conn,
                    )
                    if consumed is None and await db.peek_oauth_state_voided(
                        state_hash, "google", conn=tx_conn
                    ):
                        return None, False, True
                    return consumed, False, False

        remaining = deadline - loop.time()
        if remaining <= 0:
            return None, True, False
        await asyncio.sleep(min(poll_interval_s, remaining))


async def _claim_feishu_oauth_state_coordinated(
    state_hash: str,
    *,
    exchange_lease_seconds: int = _GOOGLE_EXCHANGE_LEASE_SECONDS,
) -> tuple[db.OAuthStateRow | None, bool]:
    """Atomically consume Feishu OAuth state & capture baseline epoch under ``lifecycle_lock_key`` on ONE connection."""
    peeked = await db.peek_oauth_state(state_hash, "feishu")
    if peeked is None:
        if await db.peek_oauth_state_voided(state_hash, "feishu"):
            return None, True
        consumed = await db.consume_oauth_state(
            state_hash, "feishu", exchange_ttl_seconds=exchange_lease_seconds
        )
        return consumed, False

    peek_uid, _peeked_client_id = peeked
    life_key = lifecycle_lock_key(peek_uid, "feishu")
    async with get_db_connection() as tx_conn, tx_conn.transaction():
        await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (life_key,))
        with track_advisory_lock("lifecycle", life_key):
            consumed = await db.consume_oauth_state(
                state_hash,
                "feishu",
                exchange_ttl_seconds=exchange_lease_seconds,
                conn=tx_conn,
            )
            if consumed is None and await db.peek_oauth_state_voided(
                state_hash, "feishu", conn=tx_conn
            ):
                return None, True
            return consumed, False


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
    exchange_lease_seconds: int = _GOOGLE_EXCHANGE_LEASE_SECONDS,
    exchange_timeout_seconds: float = _OAUTH_EXCHANGE_TOTAL_TIMEOUT_SECONDS,
) -> OAuthCallbackResult:
    """Handle the Bearer-less OAuth browser callback (§4.2(1), §4.3, §6).

    1. Step 1 (short ``pg_advisory_xact_lock`` on ONE pooled connection): atomically consume
       ``state``, capture ``baseline_*`` connection epoch, and (for Google) wait for any
       in-flight project revoke and set ``exchange_expires_at`` protection lease.
    2. Step 2 (outside all DB locks/pool connections, bounded by ``exchange_timeout_seconds``):
       exchange ``code`` and verify identity. Enforces 1-Google-account-per-Dalton-member.
    3. Step 3 (short ``pg_advisory_xact_lock`` on ONE pooled connection): atomically verify &
       consume the pending connect intent (aborting if voided by a concurrent disconnect or if
       the exchange lease expired / a revoke claimed execution), verify 1-member ownership,
       supersede old revokes, and stage or commit the connection.
    Any unactivated Google token obtained from ``exchange_code`` that does not commit or stage
    (except when rejected because another Dalton member owns the Google account) is queued for
    remote revocation.
    """
    _ = error_description  # never echo arbitrary upstream description text
    norm_provider = str(provider or "").strip().lower()
    if norm_provider not in ("google", "feishu"):
        raise OAuthStateInvalidError("Unsupported provider.", code="unsupported_provider")

    if not state or not str(state).strip():
        raise OAuthStateInvalidError("Missing OAuth state.", code="invalid_or_expired_state")

    state_hash = _sha256_hex(str(state).strip())
    intent_voided = False

    if norm_provider == "google":
        state_row, revoke_timed_out, intent_voided = await _claim_google_oauth_state_coordinated(
            state_hash,
            error=error,
            code=code,
            exchange_lease_seconds=exchange_lease_seconds,
        )
        if revoke_timed_out:
            return OAuthCallbackResult(
                status="error",
                provider=norm_provider,
                service="calendar",
                return_to=DEFAULT_INTEGRATIONS_RETURN_TO,
                error_code="revoke_in_progress",
                error_message="A previous revocation is still completing; please retry authorization.",
            )
    else:
        state_row, intent_voided = await _claim_feishu_oauth_state_coordinated(
            state_hash,
            exchange_lease_seconds=exchange_lease_seconds,
        )

    if intent_voided:
        with anyio.CancelScope(shield=True):
            await db.delete_oauth_state(state_hash, norm_provider)
        return OAuthCallbackResult(
            status="error",
            provider=norm_provider,
            service="calendar",
            return_to=DEFAULT_INTEGRATIONS_RETURN_TO,
            error_code="connection_disconnected_during_callback",
            error_message="Connection was disconnected during OAuth callback; authorization discarded.",
        )

    if state_row is None:
        raise OAuthStateInvalidError(
            "OAuth state is invalid, expired, or already used.",
            code="invalid_or_expired_state",
        )

    eff_revoker = (
        revoker
        if revoker is not None
        else (get_google_remote_revoker() if norm_provider == "google" else None)
    )
    exchanged_google_token: str | None = None
    exchanged_google_sub: str | None = None
    exchanged_google_client_id: str = state_row.oauth_client_id
    token_committed_or_staged = False
    rejected_owned_by_other_member = False
    abandon_reason = "callback_aborted_after_exchange"

    try:
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
        exchanged_google_client_id = client_id
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

        # Step 2: Upstream code -> token exchange OUTSIDE any DB lock or transaction,
        # bounded by a total timeout strictly less than the exchange protection lease.
        provider_union_id: str | None = None
        provider_email: str | None = None
        try:
            async with asyncio.timeout(max(0.05, float(exchange_timeout_seconds))):
                if norm_provider == "google":
                    g_client = google_client or get_google_oauth_client()
                    assert_no_advisory_lock_held("google.exchange_code")
                    token_resp = await g_client.exchange_code(
                        client_id=client_id,
                        client_secret=client_secret,
                        code=str(code).strip(),
                        redirect_uri=state_row.redirect_uri,
                        code_verifier=state_row.code_verifier or "",
                    )
                    # Record minted Google token immediately so post-exchange failures clean it up
                    exchanged_google_token = token_resp.refresh_token or token_resp.access_token
                    if not token_resp.id_token:
                        abandon_reason = "callback_missing_id_token"
                        return OAuthCallbackResult(
                            status="error",
                            provider=norm_provider,
                            service=state_row.service,
                            return_to=safe_return_to,
                            error_code="missing_id_token",
                            error_message="Google did not return an OIDC id_token.",
                        )
                    assert_no_advisory_lock_held("google.verify_id_token")
                    claims = await g_client.verify_id_token(
                        id_token=token_resp.id_token,
                        expected_aud=client_id,
                    )
                    provider_account_id = claims.sub
                    exchanged_google_sub = claims.sub
                    provider_email = claims.email
                    granted_list = _parse_scopes(token_resp.scope)
                else:
                    f_client = feishu_client or get_feishu_oauth_client()
                    assert_no_advisory_lock_held("feishu.exchange_code")
                    token_resp = await f_client.exchange_code(
                        app_id=client_id,
                        app_secret=client_secret,
                        code=str(code).strip(),
                        redirect_uri=state_row.redirect_uri,
                        code_verifier=state_row.code_verifier or "",
                    )
                    assert_no_advisory_lock_held("feishu.get_user_info")
                    user_info = await f_client.get_user_info(
                        access_token=token_resp.access_token
                    )
                    provider_account_id = user_info.open_id
                    provider_union_id = user_info.union_id
                    provider_email = user_info.email
                    granted_list = _parse_scopes(token_resp.scope or state_row.requested_scopes)

                    if has_capability(CapabilityId.CHANNEL_FEISHU):
                        bound_open_id, binding_app_id = await db.get_channel_feishu_binding(
                            state_row.user_id
                        )
                        if bound_open_id:
                            if not binding_app_id:
                                logger.warning(
                                    "[integration-oauth] Feishu channel binding has no recorded feishu_app_id; "
                                    "cannot confirm app ownership, skipping open_id check (rebind Feishu channel to update)"
                                )
                            elif binding_app_id == client_id:
                                if bound_open_id != provider_account_id:
                                    raise FeishuIdentityMismatchError(
                                        "Feishu OAuth account does not match your bound Feishu channel identity."
                                    )
                            else:
                                logger.info(
                                    "[integration-oauth] Feishu channel binding belongs to a different app_id; "
                                    "skipping open_id check (rebind Feishu channel to align with current app)"
                                )
        except TimeoutError:
            abandon_reason = "callback_exchange_timeout"
            return OAuthCallbackResult(
                status="error",
                provider=norm_provider,
                service=state_row.service,
                return_to=safe_return_to,
                error_code="exchange_timeout",
                error_message="OAuth token exchange timed out; please retry authorization.",
            )
        except OAuthUpstreamError as exc:
            # Exchange itself failed -> no newly minted Google token to revoke
            exchanged_google_token = None
            return OAuthCallbackResult(
                status="error",
                provider=norm_provider,
                service=state_row.service,
                return_to=safe_return_to,
                error_code=exc.error_code,
                error_message="Failed to complete OAuth token exchange with provider.",
            )
        except GoogleIdTokenVerificationError as exc:
            abandon_reason = "callback_id_token_verification_failed"
            return OAuthCallbackResult(
                status="error",
                provider=norm_provider,
                service=state_row.service,
                return_to=safe_return_to,
                error_code=exc.reason,
                error_message="Failed to verify Google identity token.",
            )

        # Decision 1: one Google (project, sub) can only connect to ONE Dalton member.
        # Reject immediately at entry without revoking the existing owner's grant.
        if norm_provider == "google":
            google_proj = extract_google_project(client_id)
            if await db.has_other_user_google_account_claim(
                google_proj,
                provider_account_id,
                exclude_user_id=state_row.user_id,
            ):
                rejected_owned_by_other_member = True
                return OAuthCallbackResult(
                    status="error",
                    provider=norm_provider,
                    service=state_row.service,
                    return_to=safe_return_to,
                    error_code="google_account_already_connected",
                    error_message=_GOOGLE_ACCOUNT_OWNED_BY_OTHER_MEMBER_MSG,
                )

        # Validate minimum required scopes (§3.3, §4.2(1))
        missing_scopes = _missing_min_scopes(norm_provider, state_row.service, granted_list)
        if missing_scopes:
            abandon_reason = "callback_missing_required_scopes"
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

        existing = await db.get_connection(
            state_row.user_id, norm_provider, state_row.service
        )
        # Prefer Step 1's snapshotted baseline epoch when available so a concurrent disconnect
        # that deleted the row during exchange_code is NEVER mistaken for a fresh first connect.
        if state_row.baseline_existed is not None:
            baseline_existed = state_row.baseline_existed
            expected_old_cid = state_row.baseline_oauth_client_id
            expected_old_sub = state_row.baseline_provider_account_id
            expected_old_epoch = state_row.baseline_auth_epoch
        else:
            baseline_existed = existing is not None
            expected_old_cid = existing.oauth_client_id if existing is not None else None
            expected_old_sub = existing.provider_account_id if existing is not None else None
            expected_old_epoch = existing.auth_epoch if existing is not None else None

        is_account_switch = (
            existing is not None and existing.provider_account_id != provider_account_id
        ) or (
            baseline_existed
            and expected_old_sub is not None
            and expected_old_sub != provider_account_id
        )

        if is_account_switch:
            if not token_resp.refresh_token:
                abandon_reason = "callback_missing_refresh_token_on_account_switch"
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
            prev_email = existing.provider_email if existing is not None else None
            prev_sub = existing.provider_account_id if existing is not None else expected_old_sub
            staged_prefs = {
                **service_prefs,
                "_account_switch_previous_email": prev_email,
                "_account_switch_previous_account_id": prev_sub,
            }
            try:
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
                    expected_old_oauth_client_id=expected_old_cid,
                    expected_old_provider_account_id=expected_old_sub,
                    expected_old_auth_epoch=expected_old_epoch,
                    consume_state_hash=state_hash,
                    baseline_existed=baseline_existed,
                )
            except GoogleAccountAlreadyConnectedByOtherMemberError as exc:
                rejected_owned_by_other_member = True
                return OAuthCallbackResult(
                    status="error",
                    provider=norm_provider,
                    service=state_row.service,
                    return_to=safe_return_to,
                    error_code=exc.code,
                    error_message=str(exc),
                )
            except (PendingConfirmationInvalidError, OAuthExchangeLeaseExpiredError) as exc:
                abandon_reason = (
                    "callback_exchange_lease_expired"
                    if isinstance(exc, OAuthExchangeLeaseExpiredError)
                    else "callback_disconnected_before_commit"
                )
                return OAuthCallbackResult(
                    status="error",
                    provider=norm_provider,
                    service=state_row.service,
                    return_to=safe_return_to,
                    error_code=exc.code,
                    error_message=str(exc),
                )
            token_committed_or_staged = True
            return OAuthCallbackResult(
                status="account_switch_confirmation_required",
                provider=norm_provider,
                service=state_row.service,
                return_to=safe_return_to,
                confirmation_token=raw_confirm,
                previous_account_email=prev_email,
                new_account_email=provider_email,
                partial_grant=partial_grant,
            )

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
            try:
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
                    expected_old_oauth_client_id=expected_old_cid,
                    expected_old_provider_account_id=expected_old_sub,
                    expected_old_auth_epoch=expected_old_epoch,
                    consume_state_hash=state_hash,
                    baseline_existed=baseline_existed,
                )
            except GoogleAccountAlreadyConnectedByOtherMemberError as exc:
                rejected_owned_by_other_member = True
                return OAuthCallbackResult(
                    status="error",
                    provider=norm_provider,
                    service=state_row.service,
                    return_to=safe_return_to,
                    error_code=exc.code,
                    error_message=str(exc),
                )
            except (PendingConfirmationInvalidError, OAuthExchangeLeaseExpiredError) as exc:
                abandon_reason = (
                    "callback_exchange_lease_expired"
                    if isinstance(exc, OAuthExchangeLeaseExpiredError)
                    else "callback_disconnected_before_commit"
                )
                return OAuthCallbackResult(
                    status="error",
                    provider=norm_provider,
                    service=state_row.service,
                    return_to=safe_return_to,
                    error_code=exc.code,
                    error_message=str(exc),
                )
            token_committed_or_staged = True
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
        # Atomically verifies & consumes the pending connect intent (state_hash) INSIDE
        # _finalize_connection's activation tx, checking voided_at, exchange lease,
        # inflight revoke claim, 1-member ownership, and baseline epoch.
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
                expected_old_oauth_client_id=expected_old_cid,
                expected_old_provider_account_id=expected_old_sub,
                expected_old_auth_epoch=expected_old_epoch,
                consume_state_hash=state_hash,
                baseline_existed=baseline_existed,
            )
        except GoogleAccountAlreadyConnectedByOtherMemberError as exc:
            rejected_owned_by_other_member = True
            return OAuthCallbackResult(
                status="error",
                provider=norm_provider,
                service=state_row.service,
                return_to=safe_return_to,
                error_code=exc.code,
                error_message=str(exc),
            )
        except (PendingConfirmationInvalidError, OAuthExchangeLeaseExpiredError) as exc:
            abandon_reason = (
                "callback_exchange_lease_expired"
                if isinstance(exc, OAuthExchangeLeaseExpiredError)
                else "callback_disconnected_before_commit"
            )
            return OAuthCallbackResult(
                status="error",
                provider=norm_provider,
                service=state_row.service,
                return_to=safe_return_to,
                error_code=exc.code,
                error_message=str(exc),
            )
        except (
            MissingRefreshTokenOnClientRotationError,
            MissingRefreshTokenError,
            MissingRefreshTokenOnAccountSwitchError,
        ) as exc:
            abandon_reason = "callback_missing_refresh_token"
            return OAuthCallbackResult(
                status="error",
                provider=norm_provider,
                service=state_row.service,
                return_to=safe_return_to,
                error_code=getattr(exc, "code", "missing_refresh_token"),
                error_message=str(exc),
            )

        token_committed_or_staged = True
        return OAuthCallbackResult(
            status="connected",
            provider=norm_provider,
            service=state_row.service,
            return_to=safe_return_to,
            connection=saved_row,
            new_account_email=provider_email,
            partial_grant=partial_grant,
        )
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await db.delete_oauth_state(state_hash, norm_provider)
            except Exception:
                pass
            if (
                norm_provider == "google"
                and exchanged_google_token is not None
                and not token_committed_or_staged
                and not rejected_owned_by_other_member
            ):
                try:
                    if exchanged_google_sub is not None:
                        await _revoke_abandoned_google_token(
                            user_id=state_row.user_id,
                            service=state_row.service,
                            oauth_client_id=exchanged_google_client_id,
                            provider_account_id=exchanged_google_sub,
                            token=exchanged_google_token,
                            reason=abandon_reason,
                            revoker=eff_revoker,
                        )
                    else:
                        proj = extract_google_project(exchanged_google_client_id)
                        if not await db.has_any_active_google_grant_in_project(proj):
                            await _revoke_abandoned_google_token(
                                user_id=state_row.user_id,
                                service=state_row.service,
                                oauth_client_id=exchanged_google_client_id,
                                provider_account_id=(
                                    state_row.expected_provider_account_id or "unverified_sub"
                                ),
                                token=exchanged_google_token,
                                reason=abandon_reason,
                                revoker=eff_revoker,
                            )
                except Exception as exc:
                    logger.warning(
                        "[integration-oauth] abandoned Google token cleanup failed (%s)",
                        type(exc).__name__,
                    )


async def _revoke_abandoned_google_token(
    *,
    user_id: str,
    service: str | None,
    oauth_client_id: str,
    provider_account_id: str,
    token: str | None,
    reason: str,
    revoker: RemoteRevoker | None = None,
) -> None:
    """Enqueue and attempt remote Google revocation for an unactivated/discarded token if no other grant exists."""
    if not token:
        return
    proj = extract_google_project(oauth_client_id)
    rkey = revoke_lock_key(proj, provider_account_id)
    revoke_pid: str | None = None
    async with get_db_connection() as tx_conn, tx_conn.transaction():
        await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (rkey,))
        with track_advisory_lock("revoke", rkey):
            if await db.has_active_or_pending_google_grant(
                proj, provider_account_id, user_id=user_id, conn=tx_conn
            ):
                return
            revoke_pid = str(uuid4())
            await db.insert_purge_ledger(
                purge_id=revoke_pid,
                user_id=user_id,
                provider="google",
                service=service,
                oauth_client_id=oauth_client_id,
                provider_account_id=provider_account_id,
                auth_epoch_upto=0,
                trigger_reason=reason,
                local_purge_status=LocalPurgeStatus.NOT_REQUIRED,
                purged_max_turn_by_thread={},
                remote_revoke_status=RemoteRevokeStatus.PENDING,
                revoke_token_plaintext=token,
                conn=tx_conn,
            )

    if revoke_pid is not None:
        eff_revoker = revoker if revoker is not None else get_google_remote_revoker()
        try:
            await process_revoke(revoke_pid, eff_revoker)
        except Exception as exc:
            logger.warning(
                "[integration-oauth] abandoned Google token revoke deferred (%s)",
                type(exc).__name__,
            )


async def _revoke_abandoned_google_pending_confirmation(
    pending: db.PendingConfirmationRow,
    *,
    reason: str,
    revoker: RemoteRevoker | None = None,
) -> None:
    """Enqueue and attempt remote Google revocation for an abandoned/cancelled unactivated confirmation ticket."""
    if pending.provider != "google":
        return
    if (pending.preferences or {}).get("_internal_commit_guard"):
        return
    tok = pending.refresh_token or pending.access_token
    await _revoke_abandoned_google_token(
        user_id=pending.user_id,
        service=pending.service,
        oauth_client_id=pending.oauth_client_id,
        provider_account_id=pending.provider_account_id,
        token=tok,
        reason=reason,
        revoker=revoker,
    )


async def confirm_pending_integration(
    *,
    user_id: str,
    confirmation_token: str,
    confirm_account_switch: bool = False,
    cancel: bool = False,
    revoker: RemoteRevoker | None = None,
) -> ConfirmIntegrationResult:
    """Confirm (or cancel) a staged Supabase, mismatched-browser local, or account-switch OAuth connection (§4.2(1)).

    Keeps the pending confirmation row persisted until the activation transaction commits
    (consuming it INSIDE the activation transaction) so there is never a window where the
    ticket is deleted while the connection is not yet committed, and failures never lose
    the staged Google refresh token.
    """
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
        await _revoke_abandoned_google_pending_confirmation(
            consumed,
            reason="pending_confirmation_cancelled",
            revoker=revoker,
        )
        return ConfirmIntegrationResult(
            status="cancelled",
            provider=consumed.provider,
            service=consumed.service,
        )

    # Peek WITHOUT consuming so an unconfirmed account-switch can return 409 safely AND
    # the pending row stays visible to revoke protection until the activation tx commits!
    preview = await db.peek_pending_confirmation(
        confirm_hash, user_id, decrypt_tokens=True
    )
    if preview is None:
        raise PendingConfirmationInvalidError(
            "Confirmation token is invalid, expired, or belongs to another user.",
        )

    require_personal_integration_capability(
        preview.provider, preview.service, mode="connect"
    )
    client_id, _secret = _load_provider_oauth_credentials(preview.provider)
    if client_id != preview.oauth_client_id:
        voided = await db.consume_pending_confirmation(confirm_hash, user_id)
        if voided is not None:
            await _revoke_abandoned_google_pending_confirmation(
                voided,
                reason="pending_confirmation_invalidated",
                revoker=revoker,
            )
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
            and existing.status
            not in (
                IntegrationStatus.DISCONNECT_PENDING,
                IntegrationStatus.REVOKED_PURGING,
            )
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
            voided = await db.consume_pending_confirmation(confirm_hash, user_id)
            if voided is not None:
                await _revoke_abandoned_google_pending_confirmation(
                    voided,
                    reason="pending_confirmation_invalidated",
                    revoker=revoker,
                )
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

    # For Google, wait outside locks if any in-flight revoke exists in the same project
    if preview.provider == "google":
        proj = extract_google_project(preview.oauth_client_id)
        settled = await _wait_for_inflight_google_revokes(proj)
        if not settled:
            raise PersonalIntegrationError(
                "A previous revocation is still completing; please retry confirmation.",
                code="revoke_in_progress",
                status_code=503,
            )

    clean_prefs = {
        k: v for k, v in (preview.preferences or {}).items() if not k.startswith("_")
    }
    eff_revoker = (
        revoker
        if revoker is not None
        else (get_google_remote_revoker() if preview.provider == "google" else None)
    )
    try:
        # Pending confirmation row is consumed atomically INSIDE _finalize_connection's tx!
        saved_row, purge_id = await _finalize_connection(
            user_id=user_id,
            provider=preview.provider,
            service=preview.service,
            oauth_client_id=preview.oauth_client_id,
            provider_account_id=preview.provider_account_id,
            provider_union_id=preview.provider_union_id,
            provider_email=preview.provider_email,
            granted_scopes=preview.granted_scopes,
            access_token=preview.access_token or "",
            refresh_token=preview.refresh_token,
            expires_at=preview.expires_at,
            preferences=clean_prefs,
            allow_account_switch=confirm_account_switch,
            revoker=eff_revoker,
            expected_old_oauth_client_id=preview.expected_old_oauth_client_id,
            expected_old_provider_account_id=preview.expected_old_provider_account_id,
            expected_old_auth_epoch=preview.expected_old_auth_epoch,
            consume_confirmation_hash=confirm_hash,
        )
    except GoogleAccountAlreadyConnectedByOtherMemberError:
        # Another Dalton member owns this (google_project, sub): drop our ticket WITHOUT revoking their grant.
        await db.consume_pending_confirmation(confirm_hash, user_id)
        raise
    except (
        PendingConfirmationInvalidError,
        MissingRefreshTokenOnClientRotationError,
        MissingRefreshTokenError,
        MissingRefreshTokenOnAccountSwitchError,
    ):
        # Transaction rolled back; pending row is still intact if not yet consumed -> consume & revoke!
        voided = await db.consume_pending_confirmation(confirm_hash, user_id)
        if voided is not None:
            await _revoke_abandoned_google_pending_confirmation(
                voided,
                reason="pending_confirmation_failed_commit",
                revoker=eff_revoker,
            )
        raise

    return ConfirmIntegrationResult(
        status="connected",
        provider=preview.provider,
        service=preview.service,
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
    expected_attempt_gen: int | None = None,
    claimed_targets: dict[str, tuple[int, int, int]] | None = None,
    conn: Any = None,
) -> FreshAccessToken:
    """Row-lock CAS commit with 3-element + per-service claimed attempt-generation check and Google sibling propagation (§4.2(2) step 3).

    Every updated row (both the initiating service and any Google sibling sharing the credential)
    is verified against its Step 1 claimed ``(auth_epoch, token_generation, refresh_attempt_gen)``.
    If a sibling was re-authorized or superseded while HTTP was in flight, its CAS matches zero
    rows and the stale refresh result is discarded without overwriting the newer credential.
    Reuses ``conn`` when provided (zero second pooled connection while holding the refresh lock).
    """
    enc_key = get_encryption_key()
    committed_row: IntegrationRow | None = None
    deferred_exc: Exception | None = None

    async def _run_commit(tx_conn: Any) -> None:
        nonlocal committed_row, deferred_exc
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

        def _eligible_claimed_sibling(sib: IntegrationRow) -> tuple[int, int, int | None] | None:
            if claimed_targets is not None:
                claimed = claimed_targets.get(sib.service)
                if claimed is None:
                    return None
                c_epoch, c_tok_gen, c_att_gen = claimed
                if (
                    sib.auth_epoch != c_epoch
                    or sib.token_generation != c_tok_gen
                    or sib.refresh_attempt_gen != c_att_gen
                ):
                    return None
                return c_epoch, c_tok_gen, c_att_gen
            if expected_attempt_gen is not None and sib.refresh_attempt_gen != expected_attempt_gen:
                return None
            return sib.auth_epoch, sib.token_generation, expected_attempt_gen

        if cur is None:
            deferred_exc = IntegrationEpochRevokedError("Connection no longer exists.")
            return

        identity_matches = (
            cur.oauth_client_id == start_client_id
            and cur.provider_account_id == start_sub
            and cur.auth_epoch == start_auth_epoch
        )
        if not identity_matches:
            deferred_exc = IntegrationEpochRevokedError(
                "Connection client, account, or auth_epoch changed during refresh."
            )
            return

        if cur.status in (
            IntegrationStatus.DISCONNECT_PENDING,
            IntegrationStatus.REVOKED_PURGING,
        ):
            # Concurrent disconnect intervened on the initiating service.
            async with tx_conn.cursor() as cur_sql:
                if provider == "feishu":
                    await cur_sql.execute(
                        """
                        UPDATE dalton_user_integrations
                        SET access_token = NULL,
                            refresh_token = NULL,
                            refresh_inflight_gen = NULL,
                            refresh_started_at = NULL,
                            refresh_inflight_until = NULL,
                            updated_at = NOW()
                        WHERE user_id = %s AND provider = %s AND service = %s
                        """,
                        (user_id, provider, service),
                    )
                else:
                    if expected_attempt_gen is None or cur.refresh_attempt_gen == expected_attempt_gen:
                        await cur_sql.execute(
                            """
                            UPDATE dalton_user_integrations
                            SET refresh_inflight_gen = NULL,
                                refresh_started_at = NULL,
                                refresh_inflight_until = NULL,
                                updated_at = NOW()
                            WHERE user_id = %s AND provider = %s AND service = %s
                            """,
                            (user_id, provider, service),
                        )
                    # Google single-service disconnect: update ONLY siblings whose claimed
                    # (auth_epoch, token_generation, refresh_attempt_gen) STILL match Step 1.
                    # Any sibling re-authorized or superseded during HTTP fails CAS and is untouched.
                    for sib in siblings:
                        cas = _eligible_claimed_sibling(sib)
                        if cas is None:
                            continue
                        c_epoch, c_tok_gen, c_att_gen = cas
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
                        attempt_clause = (
                            "AND refresh_attempt_gen = %(claimed_attempt_gen)s"
                            if c_att_gen is not None
                            else ""
                        )
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
                                refresh_inflight_until = NULL,
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
                              AND oauth_client_id = %(client_id)s
                              AND provider_account_id = %(sub)s
                              AND auth_epoch = %(auth_epoch)s
                              AND token_generation = %(token_gen)s
                              AND status IN ('connected', 'refresh_ambiguous')
                              {attempt_clause}
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
                                "client_id": start_client_id,
                                "sub": start_sub,
                                "auth_epoch": c_epoch,
                                "token_gen": c_tok_gen,
                                "claimed_attempt_gen": c_att_gen,
                            },
                        )
            deferred_exc = IntegrationEpochRevokedError(
                "Connection was disconnected during refresh."
            )
            return

        if cur.token_generation != start_token_gen or (
            expected_attempt_gen is not None
            and cur.refresh_attempt_gen != expected_attempt_gen
        ):
            # CAS generation / attempt-generation conflict branch (§4.2(2) step 3): discard stale result
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
            return

        if cur.status not in (
            IntegrationStatus.CONNECTED,
            IntegrationStatus.REFRESH_AMBIGUOUS,
        ):
            deferred_exc = IntegrationNeedsReauthError(
                "Connection requires re-authorization.",
                code=cur.last_error_code or "needs_reauth",
            )
            return

        # Normal CAS commit: authorized_at stays UNCHANGED (§4.2(2) item 5).
        rows_to_update: list[tuple[IntegrationRow, int, int, int | None]] = [
            (cur, start_auth_epoch, start_token_gen, expected_attempt_gen)
        ]
        for sib in siblings:
            cas = _eligible_claimed_sibling(sib)
            if cas is not None:
                rows_to_update.append((sib, cas[0], cas[1], cas[2]))

        async with tx_conn.cursor(row_factory=dict_row) as cur_sql:
            for target, c_epoch, c_tok_gen, c_att_gen in rows_to_update:
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

                attempt_clause = (
                    "AND refresh_attempt_gen = %(expected_attempt_gen)s"
                    if c_att_gen is not None
                    else ""
                )
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
                        refresh_inflight_until = NULL,
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
                      AND status IN ('connected', 'refresh_ambiguous')
                      {attempt_clause}
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
                        "auth_epoch": c_epoch,
                        "token_gen": c_tok_gen,
                        "expected_attempt_gen": c_att_gen,
                    },
                )
                updated_dict = await cur_sql.fetchone()
                if target.service == service and updated_dict is not None:
                    committed_row = db._row_to_integration(
                        {**dict(updated_dict), "decrypted_access_token": token_resp.access_token}
                    )

        if committed_row is None:
            deferred_exc = IntegrationEpochRevokedError("CAS update matched zero rows.")

    if conn is not None:
        await _run_commit(conn)
    else:
        async with get_db_connection() as tx_conn, tx_conn.transaction():
            await _run_commit(tx_conn)

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


def _is_refresh_lease_active(row: IntegrationRow, fallback_now: datetime) -> bool:
    """Return True if ``row`` holds an unexpired in-flight refresh lease for its current ``token_generation``."""
    if row.refresh_inflight_gen != row.token_generation:
        return False
    if row.refresh_inflight_until is None:
        return False
    ref_now = row.db_now or fallback_now
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=UTC)
    lease_utc = (
        row.refresh_inflight_until
        if row.refresh_inflight_until.tzinfo is not None
        else row.refresh_inflight_until.replace(tzinfo=UTC)
    )
    return lease_utc > ref_now


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
    """Return a valid access token for ``(user_id, provider, service)``, refreshing serially across workers (§4.2(2)).

    Follows the 3-step no-lock-across-HTTP pattern:
      1. Short lock (``refresh_lock_key``): inspect state, handle Feishu crash recovery /
         ``refresh_ambiguous`` expiry without replaying old refresh tokens, check for an
         already active in-flight lease, or claim a new ``refresh_attempt_gen`` +
         ``refresh_inflight_until`` lease, then COMMIT, UNLOCK, and RETURN DB connections.
      2. Outside all PG locks and pool connections: waiters poll DB; the sole winner invokes
         the upstream OAuth HTTP refresh client.
      3. Short lock (``refresh_lock_key``): verify identity 3-tuple, ``token_generation``,
         and ``refresh_attempt_gen``, then commit tokens/status or discard if superseded.
    """
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

    # Step 1: Short transaction-scoped advisory lock on ONE pooled connection
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
    winner_attempt_gen: int | None = None
    claimed_targets: dict[str, tuple[int, int, int]] = {}
    winner_refresh_token: str | None = None
    winner_expires_at: datetime | None = None
    winner_access_token: str | None = None
    winner_granted_scopes: str = ""
    pre_return_row: IntegrationRow | None = None
    pre_exc: Exception | None = None
    become_waiter = False

    async with get_db_connection() as pre_conn, pre_conn.transaction():
        async with pre_conn.cursor() as cur:
            await cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (r_lock_key,))
            lock_row = await cur.fetchone()
            won_short_lock = bool(lock_row and lock_row[0])

        if not won_short_lock:
            become_waiter = True
        else:
            with track_advisory_lock("refresh", r_lock_key):
                if norm_provider == "google":
                    group_rows = await db.list_connections_by_account(
                        user_id,
                        "google",
                        start_client_id,
                        start_sub,
                        decrypt_tokens=True,
                        for_update=True,
                        conn=pre_conn,
                    )
                    current = next(
                        (r for r in group_rows if r.service == norm_service),
                        None,
                    )
                    if current is None:
                        current = await db.get_connection(
                            user_id,
                            norm_provider,
                            norm_service,
                            decrypt_tokens=True,
                            for_update=True,
                            conn=pre_conn,
                        )
                    active_group = [
                        r
                        for r in group_rows
                        if r.status
                        in (
                            IntegrationStatus.CONNECTED,
                            IntegrationStatus.REFRESH_AMBIGUOUS,
                        )
                    ]
                else:
                    current = await db.get_connection(
                        user_id,
                        norm_provider,
                        norm_service,
                        decrypt_tokens=True,
                        for_update=True,
                        conn=pre_conn,
                    )
                    active_group = [current] if current is not None else []

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
                    any_lease_active = _is_refresh_lease_active(
                        current, now_inner
                    ) or any(
                        _is_refresh_lease_active(sib, now_inner)
                        for sib in active_group
                    )
                    if any_lease_active:
                        become_waiter = True
                    elif (
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
                                clear_refresh_token_only=True,
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
                    ):
                        if current.expires_at is not None and rem_inner > 0:
                            if not force_refresh and current.access_token:
                                pre_return_row = current
                            else:
                                pre_exc = IntegrationTransientError(
                                    "Feishu token refresh outcome was ambiguous; reconnect if needed.",
                                    code="refresh_ambiguous",
                                )
                        else:
                            err_code = current.last_error_code or "refresh_ambiguous_expired"
                            await db.update_connection_status(
                                user_id,
                                norm_provider,
                                norm_service,
                                status=IntegrationStatus.NEEDS_REAUTH,
                                last_error_code=err_code,
                                clear_tokens=True,
                                expected_auth_epoch=current.auth_epoch,
                                conn=pre_conn,
                            )
                            pre_exc = IntegrationNeedsReauthError(
                                "Feishu access token expired after ambiguous refresh; please reconnect.",
                                code=err_code,
                            )
                    else:
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
                            # Register ONE shared refresh attempt across the credential group
                            # and snapshot every claimed row's (auth_epoch, token_generation, shared_attempt_gen).
                            winner_token_gen = current.token_generation
                            winner_refresh_token = current.refresh_token
                            winner_expires_at = current.expires_at
                            winner_access_token = current.access_token
                            winner_granted_scopes = current.granted_scopes

                            claim_rows = (
                                active_group
                                if norm_provider == "google"
                                else [current]
                            )
                            if not any(r.service == norm_service for r in claim_rows):
                                claim_rows = [current, *claim_rows]
                            shared_attempt_gen = (
                                max(r.refresh_attempt_gen for r in claim_rows) + 1
                            )
                            claim_services = [r.service for r in claim_rows]

                            async with pre_conn.cursor() as cur_sql:
                                await cur_sql.execute(
                                    """
                                    UPDATE dalton_user_integrations
                                    SET refresh_inflight_gen = token_generation,
                                        refresh_attempt_gen = %s,
                                        refresh_started_at = NOW(),
                                        refresh_inflight_until = clock_timestamp() + make_interval(secs => %s),
                                        updated_at = NOW()
                                    WHERE user_id = %s
                                      AND provider = %s
                                      AND service = ANY(%s)
                                      AND oauth_client_id = %s
                                      AND provider_account_id = %s
                                      AND status IN ('connected', 'refresh_ambiguous')
                                    RETURNING service, auth_epoch, token_generation, refresh_attempt_gen
                                    """,
                                    (
                                        shared_attempt_gen,
                                        _REFRESH_INFLIGHT_LEASE_SECONDS,
                                        user_id,
                                        norm_provider,
                                        claim_services,
                                        start_client_id,
                                        start_sub,
                                    ),
                                )
                                claimed_rows = await cur_sql.fetchall()
                                if claimed_rows:
                                    for crow in claimed_rows:
                                        if len(crow) >= 4:
                                            claimed_targets[str(crow[0])] = (
                                                int(crow[1]),
                                                int(crow[2]),
                                                int(crow[3]),
                                            )
                                    if norm_service in claimed_targets:
                                        winner_attempt_gen = claimed_targets[norm_service][2]
                                    else:
                                        # Fallback for minimal unit-test cursor doubles returning single scalar
                                        winner_attempt_gen = shared_attempt_gen
                                        for r in claim_rows:
                                            claimed_targets[r.service] = (
                                                r.auth_epoch,
                                                r.token_generation,
                                                shared_attempt_gen,
                                            )
                                elif cur_sql.rowcount == 0:
                                    become_waiter = True

    # ALL PG advisory locks and DB pool connections are now RELEASED!
    if pre_exc is not None:
        raise pre_exc
    if pre_return_row is not None:
        _check_required_scopes(pre_return_row.granted_scopes, required_scopes)
        return _to_fresh_token(pre_return_row)
    if become_waiter:
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

    assert winner_refresh_token is not None

    # Step 2: Upstream HTTP refresh strictly OUTSIDE any PG advisory lock or DB pool connection (§4.2(2) step 2)
    committed_or_handled = False
    try:
        upstream_err: OAuthUpstreamError | None = None
        token_resp: OAuthTokenResponse | None = None
        try:
            if norm_provider == "google":
                g_client = google_client or get_google_oauth_client()
                assert_no_advisory_lock_held("google.refresh_token")
                token_resp = await g_client.refresh_token(
                    client_id=client_id,
                    client_secret=client_secret,
                    refresh_token=winner_refresh_token,
                )
            else:
                f_client = feishu_client or get_feishu_oauth_client()
                assert_no_advisory_lock_held("feishu.refresh_token")
                token_resp = await f_client.refresh_token(
                    app_id=client_id,
                    app_secret=client_secret,
                    refresh_token=winner_refresh_token,
                )
        except OAuthUpstreamError as exc:
            upstream_err = exc

        # Step 3: Re-acquire short lock on ONE pooled connection, verify claimed generations, commit!
        if token_resp is not None:
            attempts = _FEISHU_DB_COMMIT_RETRIES if norm_provider == "feishu" else 1
            last_db_exc: Exception | None = None
            for attempt in range(attempts):
                try:
                    async with get_db_connection() as commit_conn, commit_conn.transaction():
                        await commit_conn.execute(
                            "SELECT pg_advisory_xact_lock(%s)", (r_lock_key,)
                        )
                        with track_advisory_lock("refresh", r_lock_key):
                            fresh = await _commit_refresh_success_tx(
                                user_id=user_id,
                                provider=norm_provider,
                                service=norm_service,
                                start_client_id=start_client_id,
                                start_sub=start_sub,
                                start_auth_epoch=start_auth_epoch,
                                start_token_gen=winner_token_gen,
                                token_resp=token_resp,
                                required_scopes=required_scopes,
                                expected_attempt_gen=winner_attempt_gen,
                                claimed_targets=claimed_targets or None,
                                conn=commit_conn,
                            )
                            committed_or_handled = True
                            return fresh
                except PersonalIntegrationError:
                    committed_or_handled = True
                    raise
                except Exception as exc:
                    last_db_exc = exc
                    if attempt + 1 < attempts:
                        await asyncio.sleep(0.1 * (attempt + 1))
            assert last_db_exc is not None
            raise last_db_exc

        assert upstream_err is not None

        # Verify 3-element identity, token_generation & refresh_attempt_gen before mutating on upstream error.
        # Single pooled connection with pg_advisory_xact_lock; commits ALL transitions BEFORE raising!
        err_return_token: FreshAccessToken | None = None
        err_return_scopes: str | None = None
        err_to_raise: Exception | None = None

        async with get_db_connection() as err_conn, err_conn.transaction():
            await err_conn.execute("SELECT pg_advisory_xact_lock(%s)", (r_lock_key,))
            with track_advisory_lock("refresh", r_lock_key):
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
                elif latest.token_generation != winner_token_gen or (
                    winner_attempt_gen is not None
                    and latest.refresh_attempt_gen != winner_attempt_gen
                ):
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
                    async with err_conn.cursor() as cur_sql:
                        await cur_sql.execute(
                            """
                            UPDATE dalton_user_integrations
                            SET refresh_inflight_gen = NULL,
                                refresh_started_at = NULL,
                                refresh_inflight_until = NULL,
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
                    if norm_provider == "google":
                        sibling_rows = await db.list_connections_by_account(
                            user_id,
                            "google",
                            start_client_id,
                            start_sub,
                            decrypt_tokens=False,
                            for_update=True,
                            conn=err_conn,
                        )
                        targets_to_mark: list[IntegrationRow] = []
                        for r in sibling_rows:
                            if r.status not in (
                                IntegrationStatus.CONNECTED,
                                IntegrationStatus.REFRESH_AMBIGUOUS,
                            ):
                                continue
                            if claimed_targets and r.service in claimed_targets:
                                c_ep, c_tg, c_ag = claimed_targets[r.service]
                                if (
                                    r.auth_epoch != c_ep
                                    or r.token_generation != c_tg
                                    or r.refresh_attempt_gen != c_ag
                                ):
                                    continue
                            elif claimed_targets:
                                continue
                            targets_to_mark.append(r)
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
                            clear_refresh_token_only=True,
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
                    async with err_conn.cursor() as cur_sql:
                        if norm_provider == "google":
                            if winner_attempt_gen is not None:
                                await cur_sql.execute(
                                    """
                                    UPDATE dalton_user_integrations
                                    SET refresh_inflight_gen = NULL,
                                        refresh_started_at = NULL,
                                        refresh_inflight_until = NULL,
                                        last_error_code = %s,
                                        updated_at = NOW()
                                    WHERE user_id = %s
                                      AND provider = 'google'
                                      AND oauth_client_id = %s
                                      AND provider_account_id = %s
                                      AND refresh_attempt_gen = %s
                                      AND status IN ('connected', 'refresh_ambiguous')
                                    """,
                                    (
                                        upstream_err.error_code,
                                        user_id,
                                        start_client_id,
                                        start_sub,
                                        winner_attempt_gen,
                                    ),
                                )
                            else:
                                await cur_sql.execute(
                                    """
                                    UPDATE dalton_user_integrations
                                    SET refresh_inflight_gen = NULL,
                                        refresh_started_at = NULL,
                                        refresh_inflight_until = NULL,
                                        last_error_code = %s,
                                        updated_at = NOW()
                                    WHERE user_id = %s
                                      AND provider = 'google'
                                      AND oauth_client_id = %s
                                      AND provider_account_id = %s
                                      AND status IN ('connected', 'refresh_ambiguous')
                                    """,
                                    (
                                        upstream_err.error_code,
                                        user_id,
                                        start_client_id,
                                        start_sub,
                                    ),
                                )
                        else:
                            await cur_sql.execute(
                                """
                                UPDATE dalton_user_integrations
                                SET refresh_inflight_gen = NULL,
                                    refresh_started_at = NULL,
                                    refresh_inflight_until = NULL,
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
                committed_or_handled = True

        # Error transaction is now COMMITTED & lock is RELEASED. Return or raise cleanly.
        if err_return_token is not None:
            _check_required_scopes(err_return_scopes or "", required_scopes)
            return err_return_token
        if err_to_raise is not None:
            raise err_to_raise
    finally:
        if not committed_or_handled and winner_attempt_gen is not None:
            with anyio.CancelScope(shield=True):
                try:
                    await db.release_refresh_inflight_lease(
                        user_id,
                        norm_provider,
                        norm_service,
                        oauth_client_id=start_client_id,
                        provider_account_id=start_sub,
                        expected_attempt_gen=winner_attempt_gen,
                        preserve_crash_marker=(norm_provider == "feishu"),
                    )
                except Exception:
                    pass

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
    "GoogleAccountAlreadyConnectedByOtherMemberError",
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
    "OAuthExchangeLeaseExpiredError",
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
