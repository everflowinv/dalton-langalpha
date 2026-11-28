"""Authoritative OAuth callback, event route, scope, and connector constants for personal integrations.

Shared between personal integration OAuth flows, capability gating, and the
deployment credentials management API so displayed callback URLs and connector
metadata always match mounted routes (§4.3, §6).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from src.dalton.settings import get_dalton_public_app_url

GOOGLE_OAUTH_CALLBACK_PATH = "/api/v1/dalton/integrations/google/callback"
FEISHU_OAUTH_CALLBACK_PATH = "/api/v1/dalton/integrations/feishu/callback"
FEISHU_INTEGRATION_EVENTS_PATH = "/api/v1/dalton/integrations/feishu/events"

_PROVIDER_CALLBACK_PATHS: dict[str, str] = {
    "google": GOOGLE_OAUTH_CALLBACK_PATH,
    "feishu": FEISHU_OAUTH_CALLBACK_PATH,
}

# ---------------------------------------------------------------------------
# Least-privilege OAuth scopes (§3.3)
# ---------------------------------------------------------------------------

GOOGLE_SCOPE_OPENID = "openid"
GOOGLE_SCOPE_EMAIL = "email"
GOOGLE_SCOPE_CALENDAR_LIST_RO = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
)
GOOGLE_SCOPE_CALENDAR_EVENTS_RO = (
    "https://www.googleapis.com/auth/calendar.events.readonly"
)
GOOGLE_SCOPE_CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"
GOOGLE_SCOPE_CALENDAR_READONLY = (
    "https://www.googleapis.com/auth/calendar.readonly"
)
GOOGLE_SCOPE_CALENDAR_FULL = "https://www.googleapis.com/auth/calendar"
GOOGLE_SCOPE_GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_SCOPE_GMAIL_DRAFTS_CREATE = "https://www.googleapis.com/auth/gmail.drafts.create"
GOOGLE_SCOPE_GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"

GOOGLE_IDENTITY_SCOPES: tuple[str, ...] = (
    GOOGLE_SCOPE_OPENID,
    GOOGLE_SCOPE_EMAIL,
)
GOOGLE_CALENDAR_MIN_SCOPES: tuple[str, ...] = (
    GOOGLE_SCOPE_CALENDAR_LIST_RO,
    GOOGLE_SCOPE_CALENDAR_EVENTS_RO,
)
GOOGLE_GMAIL_MIN_SCOPES: tuple[str, ...] = (GOOGLE_SCOPE_GMAIL_READONLY,)
GOOGLE_GMAIL_DRAFT_SCOPES: tuple[str, ...] = (
    GOOGLE_SCOPE_GMAIL_DRAFTS_CREATE,
    GOOGLE_SCOPE_GMAIL_COMPOSE,
)

FEISHU_SCOPE_OFFLINE_ACCESS = "offline_access"
FEISHU_CALENDAR_SCOPES: tuple[str, ...] = (
    FEISHU_SCOPE_OFFLINE_ACCESS,
    "calendar:calendar:readonly",
    "calendar:calendar.event:read",
)

# Broader scopes that satisfy a narrower read-only requirement (so existing write-granted
# connections continue working seamlessly under Batch 1 read-only calendar).
SCOPE_EQUIVALENTS: dict[str, frozenset[str]] = {
    GOOGLE_SCOPE_CALENDAR_EVENTS_RO: frozenset(
        {
            GOOGLE_SCOPE_CALENDAR_EVENTS_RO,
            GOOGLE_SCOPE_CALENDAR_EVENTS,
            GOOGLE_SCOPE_CALENDAR_READONLY,
            GOOGLE_SCOPE_CALENDAR_FULL,
        }
    ),
    GOOGLE_SCOPE_CALENDAR_LIST_RO: frozenset(
        {
            GOOGLE_SCOPE_CALENDAR_LIST_RO,
            GOOGLE_SCOPE_CALENDAR_READONLY,
            GOOGLE_SCOPE_CALENDAR_FULL,
        }
    ),
    "calendar:calendar:readonly": frozenset(
        {
            "calendar:calendar:readonly",
            "calendar:calendar",
        }
    ),
    "calendar:calendar.event:read": frozenset(
        {
            "calendar:calendar.event:read",
            "calendar:calendar",
        }
    ),
}


def scope_satisfied(required_scope: str, granted_scopes: Collection[str]) -> bool:
    """Return True if ``required_scope`` (or an equivalent superset scope) is in ``granted_scopes``."""
    granted_set = set(granted_scopes)
    accepted = SCOPE_EQUIVALENTS.get(required_scope)
    if accepted is not None:
        return any(s in granted_set for s in accepted)
    return required_scope in granted_set
FEISHU_MAIL_READONLY_SCOPES: tuple[str, ...] = (
    FEISHU_SCOPE_OFFLINE_ACCESS,
    "mail:user_mailbox.folder:read",
    "mail:user_mailbox.message:readonly",
    "mail:user_mailbox.message.address:read",
    "mail:user_mailbox.message.subject:read",
    "mail:user_mailbox.message.body:read",
)
FEISHU_MAIL_SEND_SCOPE = "mail:user_mailbox.message:send"

# Feishu refresh error code classification (§4.2(2) item 5)
# Routine credential/token failures (including 20071 redirect_uri mismatch) transition to
# needs_reauth so online conversation history is preserved.
FEISHU_NEEDS_REAUTH_CODES: frozenset[int] = frozenset(
    {20024, 20063, 20037, 20005, 20064, 20071}
)
# Strict allowlist of Feishu refresh error codes officially documented as user-initiated revocation.
# Only codes in this set may trigger Scheme 2A purge from the token-refresh path (empty by default;
# Feishu delivers user-initiated revocation via the auth.user_access_token.revoked_v4 event).
FEISHU_CONFIRMED_REVOKED_CODES: frozenset[int] = frozenset()

# Official Feishu revoke_reason values representing user-initiated revocation (§4.5(3))
FEISHU_OFFICIAL_USER_REVOKE_REASONS: frozenset[str] = frozenset(
    {
        "Revoked by user action.",
    }
)

# Feishu webhook event type for user-initiated OAuth grant revocation (§3.2, §4.5(3))
FEISHU_REVOKED_EVENT_TYPE = "auth.user_access_token.revoked_v4"

# OAuth state and confirmation TTLs (§4.2(1))
OAUTH_STATE_TTL_SECONDS = 600  # strict 10 minutes
PENDING_CONFIRMATION_TTL_SECONDS = 300  # 5 minutes
HEALTH_SWEEP_INTERVAL_SECONDS = 12 * 3600  # 12 hours

DEFAULT_INTEGRATIONS_RETURN_TO = "/settings?tab=integrations"


@dataclass(frozen=True, slots=True)
class PersonalIntegrationConnectorSpec:
    """Authoritative metadata for one personal calendar or mail connector (§6)."""

    provider: Literal["google", "feishu"]
    service: Literal["calendar", "mail"]
    capability_id: str
    env_var: str
    credential_group: Literal["google_oauth", "feishu_app"]
    everflow_default: bool
    core_default: bool
    batch: Literal[1, 2, 3]
    callback_path: str
    min_required_scopes: tuple[str, ...]
    default_scopes: tuple[str, ...]
    optional_scopes: tuple[str, ...] = ()
    events_path: str | None = None


PERSONAL_INTEGRATION_CONNECTOR_SPECS: tuple[PersonalIntegrationConnectorSpec, ...] = (
    PersonalIntegrationConnectorSpec(
        provider="google",
        service="calendar",
        capability_id="google_calendar",
        env_var="DALTON_CAP_GOOGLE_CALENDAR",
        credential_group="google_oauth",
        everflow_default=True,
        core_default=False,
        batch=1,
        callback_path=GOOGLE_OAUTH_CALLBACK_PATH,
        min_required_scopes=GOOGLE_CALENDAR_MIN_SCOPES,
        default_scopes=GOOGLE_IDENTITY_SCOPES + GOOGLE_CALENDAR_MIN_SCOPES,
        optional_scopes=(),
        events_path=None,
    ),
    PersonalIntegrationConnectorSpec(
        provider="feishu",
        service="calendar",
        capability_id="feishu_calendar",
        env_var="DALTON_CAP_FEISHU_CALENDAR",
        credential_group="feishu_app",
        everflow_default=True,
        core_default=False,
        batch=1,
        callback_path=FEISHU_OAUTH_CALLBACK_PATH,
        min_required_scopes=FEISHU_CALENDAR_SCOPES,
        default_scopes=FEISHU_CALENDAR_SCOPES,
        optional_scopes=(),
        events_path=FEISHU_INTEGRATION_EVENTS_PATH,
    ),
    PersonalIntegrationConnectorSpec(
        provider="feishu",
        service="mail",
        capability_id="feishu_mail",
        env_var="DALTON_CAP_FEISHU_MAIL",
        credential_group="feishu_app",
        everflow_default=True,
        core_default=False,
        batch=2,
        callback_path=FEISHU_OAUTH_CALLBACK_PATH,
        min_required_scopes=FEISHU_MAIL_READONLY_SCOPES,
        default_scopes=FEISHU_MAIL_READONLY_SCOPES,
        optional_scopes=(FEISHU_MAIL_SEND_SCOPE,),
        events_path=FEISHU_INTEGRATION_EVENTS_PATH,
    ),
    PersonalIntegrationConnectorSpec(
        provider="google",
        service="mail",
        capability_id="google_gmail",
        env_var="DALTON_CAP_GOOGLE_GMAIL",
        credential_group="google_oauth",
        everflow_default=True,
        core_default=False,
        batch=3,
        callback_path=GOOGLE_OAUTH_CALLBACK_PATH,
        min_required_scopes=GOOGLE_GMAIL_MIN_SCOPES,
        default_scopes=GOOGLE_IDENTITY_SCOPES + GOOGLE_GMAIL_MIN_SCOPES,
        optional_scopes=GOOGLE_GMAIL_DRAFT_SCOPES,
        events_path=None,
    ),
)

_CONNECTOR_BY_PAIR: dict[tuple[str, str], PersonalIntegrationConnectorSpec] = {
    (spec.provider, spec.service): spec
    for spec in PERSONAL_INTEGRATION_CONNECTOR_SPECS
}


def get_connector_spec(
    provider: str, service: str
) -> PersonalIntegrationConnectorSpec | None:
    """Look up the authoritative connector spec for ``(provider, service)``."""
    return _CONNECTOR_BY_PAIR.get(
        (str(provider or "").strip().lower(), str(service or "").strip().lower())
    )


def build_integration_redirect_uri(provider: str) -> str:
    """Build the full OAuth callback URL for ``provider`` (``google`` or ``feishu``)."""
    normalized = str(provider or "").strip().lower()
    path = _PROVIDER_CALLBACK_PATHS.get(normalized)
    if path is None:
        raise ValueError(f"Unsupported integration provider: {provider!r}")
    origin = get_dalton_public_app_url().strip().rstrip("/")
    if not origin:
        return ""
    return f"{origin}{path}"


def build_feishu_integration_events_url() -> str:
    """Build the full Feishu personal-integration event subscription callback URL."""
    origin = get_dalton_public_app_url().strip().rstrip("/")
    if not origin:
        return ""
    return f"{origin}{FEISHU_INTEGRATION_EVENTS_PATH}"


__all__ = [
    "DEFAULT_INTEGRATIONS_RETURN_TO",
    "FEISHU_CALENDAR_SCOPES",
    "FEISHU_CONFIRMED_REVOKED_CODES",
    "FEISHU_INTEGRATION_EVENTS_PATH",
    "FEISHU_MAIL_READONLY_SCOPES",
    "FEISHU_MAIL_SEND_SCOPE",
    "FEISHU_NEEDS_REAUTH_CODES",
    "FEISHU_OAUTH_CALLBACK_PATH",
    "FEISHU_OFFICIAL_USER_REVOKE_REASONS",
    "FEISHU_REVOKED_EVENT_TYPE",
    "FEISHU_SCOPE_OFFLINE_ACCESS",
    "GOOGLE_CALENDAR_MIN_SCOPES",
    "GOOGLE_GMAIL_DRAFT_SCOPES",
    "GOOGLE_GMAIL_MIN_SCOPES",
    "GOOGLE_IDENTITY_SCOPES",
    "GOOGLE_OAUTH_CALLBACK_PATH",
    "GOOGLE_SCOPE_CALENDAR_EVENTS",
    "GOOGLE_SCOPE_CALENDAR_EVENTS_RO",
    "GOOGLE_SCOPE_CALENDAR_FULL",
    "GOOGLE_SCOPE_CALENDAR_LIST_RO",
    "GOOGLE_SCOPE_CALENDAR_READONLY",
    "GOOGLE_SCOPE_EMAIL",
    "GOOGLE_SCOPE_GMAIL_COMPOSE",
    "GOOGLE_SCOPE_GMAIL_DRAFTS_CREATE",
    "GOOGLE_SCOPE_GMAIL_READONLY",
    "GOOGLE_SCOPE_OPENID",
    "HEALTH_SWEEP_INTERVAL_SECONDS",
    "OAUTH_STATE_TTL_SECONDS",
    "PENDING_CONFIRMATION_TTL_SECONDS",
    "PERSONAL_INTEGRATION_CONNECTOR_SPECS",
    "PersonalIntegrationConnectorSpec",
    "SCOPE_EQUIVALENTS",
    "build_feishu_integration_events_url",
    "build_integration_redirect_uri",
    "get_connector_spec",
    "scope_satisfied",
]
